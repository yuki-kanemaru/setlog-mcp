"""復号した動画をブラウザで再生できる形式に変換し、ローカルへキャッシュする。

setlogの投稿動画はHEVC(QuickTime)であることが多く、そのままではブラウザで
再生できない。再生可能なMP4(H.264/AAC)へ変換したものだけをキャッシュし、
上限を超えたら古いものから削除する。鍵・署名付きURLは保存しない。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from setlog_mcp import client, media, messages

CACHE_DIR = client.STATE / "web-cache"
MAX_BYTES = int(os.environ.get("SETLOG_WEB_CACHE_BYTES", str(2 * 1024 * 1024 * 1024)))
WEB_SAFE_VIDEO = {"h264", "vp8", "vp9", "av1"}
WEB_SAFE_AUDIO = {"aac", "mp3", "opus", "vorbis", None}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# 同時に走る復号・変換を絞り、setlog側への負荷と失敗を抑える。
_CONVERT_SLOTS = threading.Semaphore(int(os.environ.get("SETLOG_WEB_CONCURRENCY", "2")))


class MediaUnavailable(Exception):
    """その投稿の動画を用意できない（削除済み・保存不可・復号不可）。"""


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _cache_key(collection_id: str, message_id: str) -> str:
    digest = hashlib.sha256(f"{collection_id}|{message_id}".encode("utf-8")).hexdigest()
    return digest[:32]


def _paths(key: str) -> tuple[Path, Path]:
    return CACHE_DIR / f"{key}.mp4", CACHE_DIR / f"{key}.jpg"


def _ffprobe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if result.returncode != 0:
        raise MediaUnavailable("動画の形式を確認できませんでした。")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise MediaUnavailable("動画の形式を確認できませんでした。") from None


def _codecs(probe: dict) -> tuple[str | None, str | None, float]:
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = 0.0
    try:
        duration = float(probe.get("format", {}).get("duration") or (video or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return (
        (video or {}).get("codec_name"),
        (audio or {}).get("codec_name"),
        duration,
    )


def _ffmpeg(arguments: list[str]) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *arguments],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise MediaUnavailable("動画の変換に失敗しました。")


def convert_to_mp4(source: Path, destination: Path) -> None:
    """再生可能な形式へ整える。既にH.264/AACならremuxのみ。"""
    probe = _ffprobe(source)
    video_codec, audio_codec, _ = _codecs(probe)
    common = ["-map", "0:v:0", "-map", "0:a:0?", "-movflags", "+faststart"]
    if video_codec in WEB_SAFE_VIDEO and audio_codec in WEB_SAFE_AUDIO:
        _ffmpeg(["-i", str(source), "-c", "copy", *common, str(destination)])
        return
    _ffmpeg(
        [
            "-i",
            str(source),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            *common,
            str(destination),
        ]
    )


def ensure_video(collection_id: str, message_id: str) -> Path:
    """再生用MP4のパスを返す。必要なら復号と変換を行う。"""
    key = _cache_key(collection_id, message_id)
    playable, _ = _paths(key)
    if playable.exists() and playable.stat().st_size > 0:
        os.utime(playable, None)
        return playable
    lock = _lock(key)
    if not lock.acquire(blocking=True, timeout=600):
        raise MediaUnavailable("動画の準備が混み合っています。")
    try:
        if playable.exists() and playable.stat().st_size > 0:
            return playable
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CONVERT_SLOTS.acquire()
        try:
            return _build(collection_id, message_id, playable)
        finally:
            _CONVERT_SLOTS.release()
    finally:
        lock.release()


def _build(collection_id: str, message_id: str, playable: Path) -> Path:
    """復号して再生可能なMP4を作る（呼び出し側でロックと同時実行数を制御する）。"""
    session = client.load_private("session.bin")
    private = messages.identity_private()
    me = messages.user_id()
    stored = messages.fetch_message(collection_id, message_id)
    if stored.get("isDeleted"):
        raise MediaUnavailable("削除された投稿です。")
    item = next((m for m in messages.media_items(stored) if m.get("path")), None)
    if item is None:
        raise MediaUnavailable("この投稿に動画はありません。")
    if item.get("permission") not in (None, "allow_saving"):
        raise MediaUnavailable("この投稿は保存が許可されていません。")
    content_key = messages.unwrap_key(stored, private, me)
    blob = media.download_media(item, session["token"])
    plain = media.decrypt_media(stored, content_key, blob)
    with tempfile.TemporaryDirectory(dir=CACHE_DIR) as temporary:
        source = Path(temporary) / "source.bin"
        source.write_bytes(plain)
        target = Path(temporary) / "converted.mp4"
        convert_to_mp4(source, target)
        if not target.exists() or target.stat().st_size == 0:
            raise MediaUnavailable("動画を準備できませんでした。")
        target.replace(playable)
    evict()
    return playable


def ensure_poster(collection_id: str, message_id: str) -> Path:
    """一覧用のサムネイルJPEGを返す。"""
    key = _cache_key(collection_id, message_id)
    _, poster = _paths(key)
    if poster.exists() and poster.stat().st_size > 0:
        os.utime(poster, None)
        return poster
    playable = ensure_video(collection_id, message_id)
    lock = _lock(key + ":poster")
    with lock:
        if poster.exists() and poster.stat().st_size > 0:
            return poster
        with tempfile.TemporaryDirectory(dir=CACHE_DIR) as temporary:
            target = Path(temporary) / "poster.jpg"
            _ffmpeg(
                [
                    "-ss",
                    "0",
                    "-i",
                    str(playable),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=400:-2:flags=lanczos",
                    "-q:v",
                    "4",
                    str(target),
                ]
            )
            target.replace(poster)
    evict()
    return poster


def evict() -> None:
    """合計サイズが上限を超えたら、古いものから削除する。"""
    if not CACHE_DIR.exists():
        return
    files = [path for path in CACHE_DIR.iterdir() if path.is_file() and path.suffix in (".mp4", ".jpg")]
    total = sum(path.stat().st_size for path in files)
    if total <= MAX_BYTES:
        return
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            continue
        if total <= MAX_BYTES:
            break


def clear() -> dict:
    removed = 0
    if CACHE_DIR.exists():
        for path in CACHE_DIR.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    return {"removed": removed}


def stats() -> dict:
    if not CACHE_DIR.exists():
        return {"files": 0, "bytes": 0, "limit": MAX_BYTES}
    files = [path for path in CACHE_DIR.iterdir() if path.is_file()]
    return {
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "limit": MAX_BYTES,
        "dir": str(CACHE_DIR),
        "updated": time.time(),
    }
