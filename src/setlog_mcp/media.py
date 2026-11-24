"""ログの投稿動画を取得・復号してローカルへ保存する。

- 対象は本人が参加しているコレクション（ログ）だけ。
- 動画は投稿ごとに個別ファイルとして保存する。
- 保存不可の設定（permission）が付いた投稿はスキップする。
- 鍵・署名付きURL・平文は応答やログへ出さない。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import client, config, crypto, history, messages, upload

# Google Drive for desktop のショートカット先。環境変数で上書きできる。
DEFAULT_SAVE_DIR = os.environ.get("SETLOG_SAVE_DIR") or (
    r"G:\.shortcut-targets-by-id\1IORvWN0zLeRCWYHeePw_aERs-mqPv4x-\setlog"
)
PAGE_SIZE = 100
REGION_BASE = {
    "ap-northeast-2": "https://api-kr.newchatapi.com/",
    "us-east-1": client.BASE,
}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(value: str, fallback: str = "unknown") -> str:
    cleaned = _UNSAFE.sub("_", (value or "").strip())[:40].strip(" .")
    return cleaned or fallback


def _iter_messages(collection_id: str, start_ms: int, end_ms: int, cap: int, token: str):
    """新しい順にメッセージを取得する。最大cap件。"""
    yield from messages.iter_messages(collection_id, start_ms, end_ms, cap, token)


def _unwrap_content_key(message: dict, identity_private: bytes, me: str) -> bytes:
    return messages.unwrap_key(message, identity_private, me)


def decrypt_media(message: dict, content_key: bytes, blob: bytes) -> bytes:
    sender = message.get("senderId")
    content_type = message.get("contentType")
    conversation = message.get("conversationId") or ""
    message_id = message.get("messageId") or ""
    version = message.get("mediaCipherV")
    versions = [int(version)] if isinstance(version, int) and version > 0 else [2, 1]
    last: Exception | None = None
    for candidate in versions:
        aad = crypto.media_aad(conversation, message_id, sender, content_type, candidate)
        try:
            return crypto.decrypt(content_key, blob, aad)
        except crypto.CryptoError as error:
            last = error
    raise last or crypto.CryptoError("動画を復号できませんでした。")


def download_media(media: dict, token: str) -> bytes:
    region = media.get("region")
    base = REGION_BASE.get(region)
    if base is None:
        raise client.SetlogError("動画の保存地域が未対応です。")
    result = client.api(
        "POST", "media/getDownloadURL", {"key": media.get("path"), "region": region}, token=token, base=base
    )
    url = result.get("downloadUrl")
    parsed = urllib.parse.urlsplit(url or "")
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".cloudfront.net"):
        raise client.SetlogError("取得したダウンロード先の形式を確認できませんでした。")
    request = urllib.request.Request(url, method="GET")
    with urllib.request.build_opener(client._NoRedirect).open(request, timeout=180) as response:
        if not 200 <= response.status < 300:
            raise client.SetlogError("動画のダウンロードに失敗しました。")
        return response.read(config.max_video_bytes() + 1)


# 既存の内部名との互換（テストと保存処理が参照する）。
_download = download_media
_decrypt_media = decrypt_media


def _remux(source: Path, destination: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        capture_output=True,
    )
    return result.returncode == 0 and destination.is_file() and destination.stat().st_size > 0


def save_log_media(
    collection_id: str,
    sender: str = "",
    start_date: str = "",
    end_date: str = "",
    output_dir: str = "",
    limit: int = 200,
    timezone_name: str = "Asia/Tokyo",
    day_start_hour: int = 4,
) -> dict:
    """ログの投稿動画を個別ファイルとして保存する。senderは表示名またはユーザーID。"""
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise client.SetlogError("limitは1〜1000を指定してください。")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise client.SetlogError("timezone_nameには有効なIANAタイムゾーン名を指定してください。") from None

    session = client.load_private("session.bin")
    identity = client.load_private("identity.bin")
    token = session["token"]
    me = client.api("GET", "user/profile", token=token).get("user", {}).get("userId")
    if not me or identity.get("userId") != me:
        raise client.SetlogError("保存済みの鍵とログイン中のアカウントが一致しません。")
    private = crypto.unb64(identity.get("identityPrivateKeyB64"))

    room = next((item for item in upload.collections() if item.get("id") == collection_id), None)
    if room is None:
        raise client.SetlogError("指定された投稿先が本人のコレクション一覧にありません。")
    profiles = room.get("memberProfiles") or {}
    names: dict[str, str] = {}
    if isinstance(profiles, dict):
        for uid, profile in profiles.items():
            if isinstance(profile, dict) and profile.get("name"):
                names[str(uid)] = str(profile["name"])

    if start_date:
        start_ms, end_ms = history.date_bounds(
            start_date, end_date or start_date, timezone_name, day_start_hour
        )
    else:
        start_ms, end_ms = 0, int(time.time() * 1000) + 1

    target = Path(output_dir).expanduser() if output_dir else Path(DEFAULT_SAVE_DIR)
    target.mkdir(parents=True, exist_ok=True)

    query = (sender or "").strip()
    saved: list[dict] = []
    skipped: list[dict] = []
    senders: dict[str, str] = {}

    for message in _iter_messages(collection_id, start_ms, end_ms, limit, token):
        if message.get("isDeleted") or message.get("conversationId", collection_id) != collection_id:
            continue
        stamp = message.get("timestamp")
        if not isinstance(stamp, (int, float)) or stamp < start_ms or stamp >= end_ms:
            continue
        sender_id = str(message.get("senderId") or "")
        name = str(message.get("senderName") or "") or names.get(sender_id, "")
        if name or sender_id:
            senders[sender_id] = name
        if query and query not in (name, sender_id):
            continue
        media = message.get("media")
        if not isinstance(media, dict) or not media.get("path"):
            continue
        permission = media.get("permission")
        if permission not in (None, "allow_saving"):
            skipped.append({"message_id": message.get("messageId"), "reason": f"保存不可({permission})"})
            continue
        identifier = str(message.get("messageId") or "")
        posted = datetime.fromtimestamp(float(stamp) / 1000, zone)
        folder = target / posted.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        base_name = f"{posted.strftime('%H%M%S')}_{_safe_name(name or sender_id)}_{identifier[:8]}"
        existing = list(folder.glob(base_name + ".*"))
        if existing:
            saved.append({"file": str(existing[0]), "message_id": identifier, "skipped": "既に保存済み"})
            continue
        try:
            content_key = _unwrap_content_key(message, private, me)
            blob = _download(media, token)
            plain = _decrypt_media(message, content_key, blob)
        except (client.SetlogError, crypto.CryptoError) as error:
            skipped.append({"message_id": identifier, "reason": str(error)})
            continue
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            source = Path(temporary) / "source.bin"
            source.write_bytes(plain)
            destination = folder / (base_name + ".mp4")
            if not _remux(source, destination):
                destination = folder / (base_name + ".mov")
                destination.write_bytes(plain)
        saved.append(
            {
                "file": str(destination),
                "message_id": identifier,
                "sender": name or sender_id,
                "posted_at": posted.isoformat(),
                "bytes": destination.stat().st_size,
            }
        )

    return {
        "collection": {"id": room.get("id"), "name": room.get("name")},
        "sender_filter": query or None,
        "saved": saved,
        "saved_count": len([item for item in saved if "skipped" not in item]),
        "skipped": skipped,
        "senders_seen": [{"id": key, "name": value} for key, value in senders.items()],
        "output_dir": str(target),
    }
