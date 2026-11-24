"""setlog の操作をWeb層向けにまとめる（読み取りはそのまま、書き込みはロック経由）。"""

from __future__ import annotations

import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from setlog_mcp import client, history, media, messages, posting, upload

from . import media_cache

DEFAULT_ZONE = "Asia/Tokyo"
DEFAULT_DAY_START = 4
UPLOAD_DIR = client.STATE / "uploads"


class BadRequest(Exception):
    """利用者の入力に起因するエラー。"""


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_ZONE)
    except Exception:
        raise BadRequest("タイムゾーンの指定を確認してください。") from None


def status() -> dict:
    state = client.STATE
    configured = True
    try:
        client.app_key()
    except client.SetlogError:
        configured = False
    info = {
        "version": _version(),
        "api_key_configured": configured,
        "session_present": (state / "session.bin").exists(),
        "identity_present": (state / "identity.bin").exists(),
        "state_directory": str(state),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "cache": media_cache.stats(),
    }
    if info["session_present"] and info["identity_present"]:
        try:
            info["profile"] = _profile()
            info["authenticated"] = True
        except (client.SetlogError, ValueError) as error:
            info["authenticated"] = False
            info["error"] = str(error)
    else:
        info["authenticated"] = False
    return info


def _version() -> str:
    from setlog_mcp import __version__

    return __version__


def _profile() -> dict:
    user = messages.profile()
    return {
        "user_id": user.get("userId"),
        "name": user.get("name") or user.get("userName") or "",
        "avatar_url": user.get("avatarUrl"),
        "tint_color": user.get("tintColor"),
    }


def collections_view() -> dict:
    rooms = upload.collections()
    me = messages.user_id()
    items = []
    for room in rooms:
        participants = [p for p in (room.get("participants") or []) if isinstance(p, dict)]
        names = {str(p.get("userId")): p.get("name") or "" for p in participants if p.get("userId")}
        profiles = room.get("memberProfiles") or {}
        if isinstance(profiles, dict):
            for uid, value in profiles.items():
                if isinstance(value, dict) and value.get("name"):
                    names[str(uid)] = value["name"]
        names[me] = ""
        items.append(
            {
                "id": room.get("id"),
                "name": room.get("name") or "名称未設定のLog",
                "participants": len(participants),
                "members": [
                    {"id": p.get("userId"), "name": p.get("name") or "", "avatar": p.get("avatarUrl")}
                    for p in participants
                ],
                "locked": bool(room.get("roomLocked")),
                "last_read": room.get("lastReadTimestamp"),
            }
        )
    items.sort(key=lambda item: item["name"])
    return {"collections": items, "me": me, "profile": _profile()}


def feed(
    collection_id: str,
    date: str = "",
    before: int | None = None,
    limit: int = 40,
    timezone: str = DEFAULT_ZONE,
    day_start_hour: int = DEFAULT_DAY_START,
) -> dict:
    zone = _zone(timezone)
    start_ms = end_ms = None
    if date:
        try:
            start_ms, end_ms = history.date_bounds(date, date, timezone, int(day_start_hour))
        except client.SetlogError as error:
            raise BadRequest(str(error)) from None
    result = messages.read_feed(
        collection_id,
        limit=limit,
        before_ms=before,
        timezone_name=timezone,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    me = messages.user_id()
    posts = result["posts"]
    by_id = {post["message_id"]: post for post in posts}
    for post in posts:
        post["is_reply"] = bool(post.get("parent_id")) and post.get("parent_id") in by_id
        post["has_video"] = any(
            item.get("path") and (item.get("mime") or "").startswith("video") for item in post["media"]
        )
        post["has_image"] = any(
            item.get("path") and (item.get("mime") or "").startswith("image") for item in post["media"]
        )
        post["unread"] = (not post["is_mine"]) and me not in post.get("read_by", [])
        post["poster_url"] = (
            f"/api/media/{collection_id}/{post['message_id']}/poster" if post["has_video"] else None
        )
        post["video_url"] = f"/api/media/{collection_id}/{post['message_id']}" if post["has_video"] else None
        post["own_reactions"] = [r["text"] for r in post.get("reactions", []) if r.get("user_id") == me]
    return {
        "posts": posts,
        "next_before": result.get("next_before"),
        "collection_id": collection_id,
        "date": date or datetime.now(zone).date().isoformat(),
        "timezone": timezone,
        "day_start_hour": day_start_hour,
        "me": me,
    }


def video_path(collection_id: str, message_id: str) -> Path:
    return media_cache.ensure_video(collection_id, message_id)


def poster_path(collection_id: str, message_id: str) -> Path:
    return media_cache.ensure_poster(collection_id, message_id)


def _locked(function, *args, **kwargs):
    try:
        with client.operation_lock():
            return function(*args, **kwargs)
    except client.StateBusyError as error:
        raise Busy(str(error)) from None


class Busy(Exception):
    """他のプロセスが同じ保存先を使用中。"""


def react(collection_id: str, message_id: str, text: str, remove: bool) -> dict:
    return _locked(messages.set_reaction, collection_id, message_id, text, remove=remove)


def reply(collection_id: str, text: str, parent_id: str = "") -> dict:
    return _locked(messages.send_text, collection_id, text, parent_id)


def delete_message(collection_id: str, message_id: str) -> dict:
    return _locked(messages.delete_message, collection_id, message_id)


def reply_thread(collection_id: str, parent_id: str, limit: int = 50) -> dict:
    result = messages.read_feed(collection_id, limit=limit)
    replies = [post for post in result["posts"] if post.get("parent_id") == parent_id]
    replies.sort(key=lambda item: item["timestamp_ms"])
    return {"replies": replies}


def upload_file(name: str, data: bytes) -> dict:
    if not data:
        raise BadRequest("ファイルが空です。")
    if len(data) > client_max_bytes():
        raise BadRequest("動画が設定された上限を超えています。")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    suffix = Path(name or "upload.mp4").suffix.lower() or ".mp4"
    # 変換元と変換先を別ファイルにする（同じパスだとffmpegが失敗する）。
    raw = UPLOAD_DIR / f"{identifier}.source{suffix}"
    raw.write_bytes(data)
    target = UPLOAD_DIR / f"{identifier}.mp4"
    try:
        media_cache.convert_to_mp4(raw, target)
    finally:
        raw.unlink(missing_ok=True)
    return {
        "upload_id": identifier,
        "path": str(target),
        "name": name,
        "bytes": target.stat().st_size,
    }


def client_max_bytes() -> int:
    from setlog_mcp import config

    return config.max_video_bytes() * 2


def resolve_upload(upload_id: str) -> Path:
    if not upload_id or not all(character in "0123456789abcdef" for character in upload_id):
        raise BadRequest("アップロードIDの形式を確認してください。")
    path = UPLOAD_DIR / f"{upload_id}.mp4"
    if not path.exists():
        raise BadRequest("アップロードした動画が見つかりません。")
    return path


def prepare(collection_id: str, path: str, comment: str = "") -> dict:
    return _locked(upload.prepare, Path(path), collection_id, comment)


def send(job_id: str) -> dict:
    return _locked(posting.send_one, job_id)


def verify(job_id: str) -> dict:
    return _locked(upload.verify_job, job_id)


def jobs(limit: int = 30) -> dict:
    items = []
    for path in client.STATE.glob("job-*.bin"):
        try:
            job = client.load_private(path.name)
        except Exception:
            continue
        message = job.get("message") or {}
        items.append(
            {
                "job_id": path.name[len("job-") : -len(".bin")],
                "phase": job.get("phase"),
                "collection": job.get("collection"),
                "collection_name": job.get("collectionName"),
                "file_name": job.get("fileName"),
                "timestamp_ms": message.get("clientTimestamp"),
                "raw_sha256": job.get("originalSha256"),
                "server_message_id": job.get("serverMessageId"),
            }
        )
    items.sort(key=lambda item: item.get("timestamp_ms") or 0, reverse=True)
    return {"jobs": items[:limit], "total": len(items)}


def schedules(include_finished: bool = False) -> dict:
    return posting.list_scheduled_posts(include_finished, 100)


def schedule(job_ids: list[str], publish_at: str, interval_seconds: int = 0) -> dict:
    return _locked(posting.schedule_videos, job_ids, publish_at, int(interval_seconds))


def cancel_schedule(schedule_id: str) -> dict:
    return _locked(posting.cancel_scheduled_post, schedule_id)


def save_media(
    collection_id: str, sender: str = "", start_date: str = "", end_date: str = "", output_dir: str = ""
) -> dict:
    return _locked(
        media.save_log_media,
        collection_id,
        sender,
        start_date,
        end_date,
        output_dir,
    )


def default_save_dir() -> str:
    return str(media.DEFAULT_SAVE_DIR)


def day_options(timezone: str = DEFAULT_ZONE, day_start_hour: int = DEFAULT_DAY_START) -> list[dict]:
    zone = _zone(timezone)
    today = datetime.now(zone).date()
    if day_start_hour > 0 and datetime.now(zone).hour < day_start_hour:
        today = today - timedelta(days=1)
    return [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "label": (today - timedelta(days=offset)).strftime("%m/%d"),
            "weekday": "月火水木金土日"[(today - timedelta(days=offset)).weekday()],
        }
        for offset in range(0, 14)
    ]


def now_iso(timezone: str = DEFAULT_ZONE) -> str:
    return datetime.now(_zone(timezone)).isoformat(timespec="seconds")


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _unused(*_args) -> None:
    time.sleep(0)
