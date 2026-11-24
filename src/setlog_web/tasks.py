"""長時間かかる処理をバックグラウンドで実行し、進捗を問い合わせできるようにする。"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable

_TASKS: dict[str, dict] = {}
_GUARD = threading.Lock()
MAX_KEPT = 40


def submit(kind: str, function: Callable[[], Any]) -> str:
    identifier = uuid.uuid4().hex
    record = {
        "task_id": identifier,
        "kind": kind,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    }
    with _GUARD:
        _TASKS[identifier] = record
        _prune()

    def run() -> None:
        try:
            record["result"] = function()
            record["status"] = "done"
        except Exception as error:  # noqa: BLE001 - 画面に理由を出すため
            record["status"] = "failed"
            record["error"] = _message(error)
            record["trace"] = traceback.format_exc(limit=3)
        finally:
            record["finished_at"] = time.time()

    thread = threading.Thread(target=run, name=f"setlog-web-{kind}-{identifier[:8]}", daemon=True)
    thread.start()
    return identifier


def get(identifier: str) -> dict | None:
    with _GUARD:
        record = _TASKS.get(identifier)
        return dict(record) if record else None


def _prune() -> None:
    if len(_TASKS) <= MAX_KEPT:
        return
    finished = sorted(
        (item for item in _TASKS.values() if item["status"] != "running"),
        key=lambda item: item["started_at"],
    )
    for item in finished[: len(_TASKS) - MAX_KEPT]:
        _TASKS.pop(item["task_id"], None)


def _message(error: Exception) -> str:
    from setlog_mcp import client, crypto

    if isinstance(error, (client.SetlogError, crypto.CryptoError)):
        return str(error)
    from .service import BadRequest, Busy

    if isinstance(error, (BadRequest, Busy)):
        return str(error)
    return "処理を完了できませんでした。設定と保存状態を確認してください。"
