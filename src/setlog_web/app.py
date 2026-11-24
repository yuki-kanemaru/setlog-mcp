"""setlogをPCのブラウザから操作するローカルWebアプリ。"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from setlog_mcp import client, crypto

from . import media_cache, service, tasks

TOKEN = secrets.token_urlsafe(24)
COOKIE = "setlog_web"
MAX_JSON_BYTES = 64 * 1024


def asset_dir() -> Path:
    """画面の静的ファイルを探す。"""
    candidates = []
    override = os.environ.get("SETLOG_WEB_DIR")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path(__file__).resolve().parents[2] / "web")
    candidates.append(Path.cwd() / "web")
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    raise RuntimeError("画面のファイル（web/index.html）が見つかりません。SETLOG_WEB_DIRを指定してください。")


def _json(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def _fail(error: Exception) -> JSONResponse:
    if isinstance(error, (service.BadRequest, client.SetlogError, crypto.CryptoError)):
        text = str(error)
        if "HTTP 403" in text:
            return _json(
                {
                    "error": "setlog側に一時的に拒否されました（認証または利用制限）。時間をおいて更新してください。",
                    "detail": text[:200],
                },
                503,
            )
        return _json({"error": text}, 400)
    if isinstance(error, service.Busy):
        return _json({"error": str(error), "busy": True}, 409)
    if isinstance(error, media_cache.MediaUnavailable):
        return _json({"error": str(error)}, 404)
    return _json({"error": "処理を完了できませんでした。状態とログを確認してください。"}, 500)


def _authorized(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE, "")
    header = request.headers.get("x-setlog-token", "")
    return secrets.compare_digest(cookie, TOKEN) or secrets.compare_digest(header, TOKEN)


async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        if not _authorized(request):
            return _json({"error": "この画面から開き直してください。"}, 403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-setlog-token", ""), TOKEN):
                return _json({"error": "操作の検証に失敗しました。画面を再読み込みしてください。"}, 403)
    return await call_next(request)


class GuardMiddleware(BaseHTTPMiddleware):
    """cookie とヘッダーのトークンを検証するミドルウェア。"""

    async def dispatch(self, request: Request, call_next):
        return await guard(request, call_next)


async def index(request: Request) -> Response:
    html = (asset_dir() / "index.html").read_text(encoding="utf-8")
    html = html.replace("__SETLOG_TOKEN__", TOKEN)
    response = HTMLResponse(html)
    response.set_cookie(COOKIE, TOKEN, httponly=True, samesite="strict", path="/")
    return response


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_JSON_BYTES:
        raise service.BadRequest("リクエストが大きすぎます。")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise service.BadRequest("JSONの形式を確認してください。") from None
    if not isinstance(value, dict):
        raise service.BadRequest("JSONの形式を確認してください。")
    return value


def _need(body: dict, *names: str) -> list[str]:
    values = []
    for name in names:
        value = body.get(name)
        if not isinstance(value, str) or not value.strip():
            raise service.BadRequest(f"{name} が不足しています。")
        values.append(value.strip())
    return values


async def api_status(request: Request) -> Response:
    try:
        return _json(await asyncio.to_thread(service.status))
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_collections(request: Request) -> Response:
    try:
        return _json(await asyncio.to_thread(service.collections_view))
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_feed(request: Request) -> Response:
    params = request.query_params
    collection = params.get("collection", "")
    if not collection:
        return _json({"error": "collection が不足しています。"}, 400)
    try:
        before = int(params["before"]) if params.get("before") else None
    except ValueError:
        return _json({"error": "before の形式を確認してください。"}, 400)
    try:
        limit = int(params.get("limit") or 40)
    except ValueError:
        return _json({"error": "limit の形式を確認してください。"}, 400)
    try:
        payload = await asyncio.to_thread(
            service.feed,
            collection,
            params.get("date", ""),
            before,
            max(1, min(limit, 100)),
            params.get("timezone", service.DEFAULT_ZONE),
            int(params.get("day_start_hour") or service.DEFAULT_DAY_START),
        )
        return _json(payload)
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_video(request: Request) -> Response:
    collection = request.path_params["collection"]
    message = request.path_params["message"]
    try:
        path = await asyncio.to_thread(service.video_path, collection, message)
    except Exception as error:  # noqa: BLE001
        return _fail(error)
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "private, max-age=3600"},
    )


async def api_poster(request: Request) -> Response:
    collection = request.path_params["collection"]
    message = request.path_params["message"]
    try:
        path = await asyncio.to_thread(service.poster_path, collection, message)
    except Exception as error:  # noqa: BLE001
        return _fail(error)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


async def api_reactions(request: Request) -> Response:
    try:
        body = await _body(request)
        collection, message, text = _need(body, "collection_id", "message_id", "text")
        result = await asyncio.to_thread(service.react, collection, message, text, bool(body.get("remove")))
        return _json(result)
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_replies(request: Request) -> Response:
    try:
        body = await _body(request)
        collection, text = _need(body, "collection_id", "text")
        parent = body.get("parent_id") or ""
        if parent and not isinstance(parent, str):
            raise service.BadRequest("parent_id の形式を確認してください。")
        result = await asyncio.to_thread(service.reply, collection, text, parent)
        return _json(result)
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_thread(request: Request) -> Response:
    params = request.query_params
    try:
        collection, parent = params.get("collection", ""), params.get("parent", "")
        if not collection or not parent:
            raise service.BadRequest("collection と parent が必要です。")
        return _json(await asyncio.to_thread(service.reply_thread, collection, parent))
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_delete(request: Request) -> Response:
    try:
        body = await _body(request)
        collection, message = _need(body, "collection_id", "message_id")
        return _json(await asyncio.to_thread(service.delete_message, collection, message))
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_upload(request: Request) -> Response:
    name = request.query_params.get("name") or "upload.mp4"
    raw = await request.body()
    try:
        result = await asyncio.to_thread(service.upload_file, name, raw)
        return _json(result)
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_prepare(request: Request) -> Response:
    try:
        body = await _body(request)
        collection, comment = body.get("collection_id"), body.get("comment") or ""
        if not isinstance(collection, str) or not collection:
            raise service.BadRequest("collection_id が不足しています。")
        if not isinstance(comment, str):
            raise service.BadRequest("comment の形式を確認してください。")
        if body.get("upload_id"):
            path = service.resolve_upload(str(body["upload_id"]))
        elif body.get("path"):
            path = Path(str(body["path"]))
        else:
            raise service.BadRequest("動画を指定してください。")
        task = tasks.submit("prepare", lambda: service.prepare(collection, str(path), comment))
        return _json({"task_id": task})
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_send(request: Request) -> Response:
    job_id = request.path_params["job_id"]
    task = tasks.submit("send", lambda: service.send(job_id))
    return _json({"task_id": task})


async def api_verify(request: Request) -> Response:
    job_id = request.path_params["job_id"]
    task = tasks.submit("verify", lambda: service.verify(job_id))
    return _json({"task_id": task})


async def api_jobs(request: Request) -> Response:
    try:
        return _json(await asyncio.to_thread(service.jobs))
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_task(request: Request) -> Response:
    record = tasks.get(request.path_params["task_id"])
    if record is None:
        return _json({"error": "その処理は見つかりません。"}, 404)
    return _json(record)


async def api_schedules(request: Request) -> Response:
    try:
        if request.method == "GET":
            include = request.query_params.get("include_finished") == "1"
            return _json(await asyncio.to_thread(service.schedules, include))
        body = await _body(request)
        ids = body.get("job_ids")
        publish_at = body.get("publish_at")
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise service.BadRequest("job_ids を指定してください。")
        if not isinstance(publish_at, str) or not publish_at:
            raise service.BadRequest("publish_at を指定してください。")
        interval = int(body.get("interval_seconds") or 0)
        return _json(await asyncio.to_thread(service.schedule, ids, publish_at, interval))
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_cancel_schedule(request: Request) -> Response:
    try:
        return _json(await asyncio.to_thread(service.cancel_schedule, request.path_params["schedule_id"]))
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_save_media(request: Request) -> Response:
    try:
        body = await _body(request)
        collection = body.get("collection_id")
        if not isinstance(collection, str) or not collection:
            raise service.BadRequest("collection_id が不足しています。")
        sender = str(body.get("sender") or "")
        start_date = str(body.get("start_date") or "")
        end_date = str(body.get("end_date") or "")
        output_dir = str(body.get("output_dir") or "")
        task = tasks.submit(
            "save-media",
            lambda: service.save_media(collection, sender, start_date, end_date, output_dir),
        )
        return _json({"task_id": task})
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_cache(request: Request) -> Response:
    return _json({"cache": media_cache.stats(), "save_dir": service.default_save_dir()})


async def api_clear_cache(request: Request) -> Response:
    return _json(media_cache.clear())


async def api_options(request: Request) -> Response:
    try:
        return _json(
            {
                "days": await asyncio.to_thread(service.day_options),
                "save_dir": service.default_save_dir(),
                "ffmpeg": service.ffmpeg_available(),
                "now": service.now_iso(),
            }
        )
    except Exception as error:  # noqa: BLE001
        return _fail(error)


async def api_health(request: Request) -> Response:
    return _json({"ok": True})


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/health", api_health),
        Route("/api/status", api_status),
        Route("/api/options", api_options),
        Route("/api/collections", api_collections),
        Route("/api/feed", api_feed),
        Route("/api/thread", api_thread),
        Route("/api/reactions", api_reactions, methods=["POST"]),
        Route("/api/replies", api_replies, methods=["POST"]),
        Route("/api/messages/delete", api_delete, methods=["POST"]),
        Route("/api/uploads", api_upload, methods=["POST"]),
        Route("/api/jobs", api_jobs),
        Route("/api/jobs/prepare", api_prepare, methods=["POST"]),
        Route("/api/jobs/{job_id}/send", api_send, methods=["POST"]),
        Route("/api/jobs/{job_id}/verify", api_verify, methods=["POST"]),
        Route("/api/schedules", api_schedules, methods=["GET", "POST"]),
        Route("/api/schedules/{schedule_id}/cancel", api_cancel_schedule, methods=["POST"]),
        Route("/api/save-media", api_save_media, methods=["POST"]),
        Route("/api/tasks/{task_id}", api_task),
        Route("/api/cache", api_cache),
        Route("/api/cache/clear", api_clear_cache, methods=["POST"]),
        Route("/api/media/{collection}/{message}/poster", api_poster),
        Route("/api/media/{collection}/{message}", api_video),
        Mount("/static", StaticFiles(directory=str(asset_dir())), name="static"),
    ],
    middleware=[Middleware(GuardMiddleware)],
)
