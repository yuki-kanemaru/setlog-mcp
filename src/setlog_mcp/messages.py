"""チャット（テキスト返信）・リアクション・投稿の読み取りと送信。

APKの `chat/message/addReaction` / `removeReaction` と、`type=chat` の
暗号化テキストメッセージ（`envelope` + 受信者ごとの `keys`）を使う。
鍵・平文・署名付きURLは応答に含めない。
"""

from __future__ import annotations

import os
import time
import urllib.parse
import uuid
from datetime import datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import client, crypto, upload

TEXT_TYPE = "chat"
TEXT_CONTENT_TYPE = "text/plain"
MEDIA_KEY_AAD_CONTENT_TYPE = "application/octet-stream"
MAX_TEXT_LENGTH = 500
REACTION_LIMIT = 200
ROOM_TTL_SECONDS = 30.0
_ROOMS: dict[str, tuple[float, dict]] = {}


def user_id() -> str:
    return client.load_private("session.bin")["user"]["userId"]


def profile() -> dict:
    return client.api("GET", "user/profile", token=client.load_private("session.bin")["token"]).get(
        "user", {}
    )


def identity_private() -> bytes:
    identity = client.load_private("identity.bin")
    me = user_id()
    if identity.get("userId") != me:
        raise client.SetlogError("保存済みの暗号鍵とログイン中のアカウントが一致しません。")
    private = crypto.unb64(identity.get("identityPrivateKeyB64"))
    if crypto.key_id(crypto.public_key(private)) != identity.get("kid"):
        raise client.SetlogError("保存済みの暗号鍵を検証できませんでした。")
    return private


def room(collection_id: str) -> dict:
    cached = _ROOMS.get(collection_id)
    if cached is not None and time.time() - cached[0] < ROOM_TTL_SECONDS:
        return cached[1]
    found = next((item for item in upload.collections() if item.get("id") == collection_id), None)
    if found is None:
        raise client.SetlogError("指定された投稿先が本人のコレクション一覧にありません。")
    _ROOMS[collection_id] = (time.time(), found)
    return found


def forget_rooms() -> None:
    _ROOMS.clear()


def unwrap_key(message: dict, private: bytes, me: str) -> bytes:
    """自分宛ての鍵を開いてメッセージの内容鍵を取り出す。"""
    keys = message.get("keys")
    if not isinstance(keys, list):
        raise client.SetlogError("投稿の鍵情報を確認できませんでした。")
    entry = next((item for item in keys if isinstance(item, dict) and item.get("uid") == me), None)
    if entry is None:
        raise client.SetlogError("自分宛ての鍵がない投稿です。")
    sender = message.get("senderId")
    if not isinstance(sender, str) or not sender:
        raise client.SetlogError("投稿の送信者を確認できませんでした。")
    aad = crypto.message_aad(
        message.get("conversationId") or "", "media-key", sender, MEDIA_KEY_AAD_CONTENT_TYPE, 1
    )
    wrapping = crypto.derive(
        private,
        crypto.unb64(entry.get("senderPublicKey")),
        crypto.unb64(entry.get("saltB64")),
        aad,
    )
    return crypto.decrypt(wrapping, crypto.unb64(entry.get("wrappedB64")), aad)


def decrypt_text(message: dict, private: bytes, me: str) -> str:
    """暗号化されたテキスト本文を復号する。"""
    envelope = message.get("envelope")
    if not isinstance(envelope, dict):
        return ""
    if envelope.get("v") != 1 or envelope.get("alg") not in (None, "CHACHA20-POLY1305"):
        raise client.SetlogError("未対応のテキスト暗号化形式です。")
    aad = crypto.unb64(envelope.get("aadB64"))
    plain = crypto.decrypt(unwrap_key(message, private, me), crypto.unb64(envelope.get("combinedB64")), aad)
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise client.SetlogError("復号した本文の文字コードを確認できませんでした。") from None


def _stamp(message: dict) -> int:
    value = message.get("timestamp")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise client.SetlogError("取得した投稿の日時を確認できませんでした。")
    return int(value)


def media_items(message: dict) -> list[dict]:
    media = message.get("media")
    if isinstance(media, dict):
        return [media] if media.get("path") or media.get("mimeType") else list(media.values())
    if isinstance(media, list):
        return [item for item in media if isinstance(item, dict)]
    return []


def reactions(message: dict) -> list[dict]:
    value = message.get("reactions")
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text:
            continue
        result.append(
            {
                "text": text[:16],
                "user_id": item.get("userId") if isinstance(item.get("userId"), str) else None,
                "timestamp": item.get("timestamp")
                if isinstance(item.get("timestamp"), (int, float))
                else None,
            }
        )
    return result


def summarize(message: dict, me: str, private: bytes, names: dict[str, str], zone: ZoneInfo) -> dict:
    """Web層へ渡す投稿の要約。復号できない本文は理由つきで空にする。"""
    identifier = message.get("messageId") or message.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise client.SetlogError("取得した投稿のIDを確認できませんでした。")
    sender = message.get("senderId") if isinstance(message.get("senderId"), str) else ""
    stamp = _stamp(message)
    text = ""
    text_error = None
    if message.get("contentType") == TEXT_CONTENT_TYPE and isinstance(message.get("envelope"), dict):
        try:
            text = decrypt_text(message, private, me)
        except (client.SetlogError, crypto.CryptoError, ValueError) as error:
            text_error = str(error)
    items = media_items(message)
    return {
        "message_id": identifier,
        "sender_id": sender,
        "sender_name": names.get(sender) or ("自分" if sender == me else ""),
        "is_mine": sender == me,
        "timestamp_ms": stamp,
        "posted_at": datetime.fromtimestamp(stamp / 1000, zone).isoformat(),
        "kind": "chat" if message.get("contentType") == TEXT_CONTENT_TYPE else "media",
        "text": text,
        "text_error": text_error,
        "media": [
            {
                "mime": item.get("mimeType"),
                "path": item.get("path"),
                "region": item.get("region"),
                "permission": item.get("permission"),
                "width": (item.get("dimensions") or {}).get("width"),
                "height": (item.get("dimensions") or {}).get("height"),
                "has_audio": item.get("hasAudio"),
                "captions": [
                    caption.get("text", "")
                    for caption in (item.get("captions") or [])
                    if isinstance(caption, dict) and isinstance(caption.get("text"), str)
                ],
            }
            for item in items
        ],
        "reactions": reactions(message),
        "parent_id": message.get("parentId") or message.get("replyParentMessageId") or "",
        "read_by": [uid for uid in (message.get("readBy") or []) if isinstance(uid, str)],
        "saved_by": [uid for uid in (message.get("savedBy") or []) if isinstance(uid, str)],
    }


def member_names(collection_id: str) -> dict[str, str]:
    """参加者名の対応表。一覧のparticipantsとmemberProfilesの両方から集める。"""
    item = room(collection_id)
    names: dict[str, str] = {}
    for participant in item.get("participants") or []:
        if not isinstance(participant, dict):
            continue
        uid = participant.get("userId")
        name = participant.get("name") or participant.get("userName")
        if isinstance(uid, str) and isinstance(name, str) and name:
            names[uid] = name
    profiles = item.get("memberProfiles")
    if isinstance(profiles, dict):
        for uid, value in profiles.items():
            if isinstance(value, dict):
                name = value.get("name") or value.get("userName")
                if isinstance(name, str) and name:
                    names[str(uid)] = name
    return names


def iter_messages(collection_id: str, start_ms: int, end_ms: int, cap: int, token: str) -> Iterator[dict]:
    """新しい順にメッセージを取得する。最大cap件。"""
    cursor = ""
    seen = 0
    while seen < cap:
        limit = min(100, cap - seen)
        query = (
            {"limit": limit, "beforeTimestamp": end_ms} if not cursor else {"limit": limit, "cursor": cursor}
        )
        path = (
            "chat/"
            + urllib.parse.quote(collection_id, safe="")
            + "/messages?"
            + urllib.parse.urlencode(query)
        )
        response = client.api("GET", path, token=token)
        messages = response.get("messages")
        if not isinstance(messages, list):
            raise client.SetlogError("投稿一覧の形式を確認できませんでした。")
        if not messages:
            return
        for message in messages:
            if not isinstance(message, dict):
                raise client.SetlogError("投稿一覧の形式を確認できませんでした。")
            seen += 1
            yield message
        if not response.get("hasMore"):
            return
        next_cursor = response.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            return
        cursor = next_cursor
        oldest = min(int(message.get("timestamp", 0)) for message in messages)
        if oldest < start_ms:
            return


def read_feed(
    collection_id: str,
    limit: int = 40,
    before_ms: int | None = None,
    timezone_name: str = "Asia/Tokyo",
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict:
    """投稿一覧を新しい順で返す。end_ms は排他的上限。

    一覧APIは1ページ内を古い順で返し、指定時刻より前をさかのぼる。そのため
    ページ全体を集めてから並べ替え、新しいものからlimit件を残す。
    """
    if not 1 <= limit <= 100:
        raise client.SetlogError("limitは1〜100を指定してください。")
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        raise client.SetlogError("timezone_nameには有効なIANAタイムゾーン名を指定してください。") from None
    token = client.load_private("session.bin")["token"]
    me = user_id()
    private = identity_private()
    names = member_names(collection_id)
    # 端末時計よりサーバー時刻が進む場合があるため、無指定では余裕を持たせる。
    upper = (
        end_ms
        if end_ms is not None
        else (before_ms if before_ms is not None else int(time.time() * 1000) + 3_600_000)
    )
    lower = start_ms if start_ms is not None else 0
    rows, reached, seen = [], False, set()
    for message in iter_messages(collection_id, lower, upper, limit * 3, token):
        if message.get("isDeleted"):
            continue
        if message.get("conversationId", collection_id) != collection_id:
            continue
        identifier = message.get("messageId") or message.get("id")
        if identifier in seen:
            continue
        stamp = _stamp(message)
        if stamp >= upper:
            continue
        if stamp < lower:
            reached = True
            break
        seen.add(identifier)
        rows.append(summarize(message, me, private, names, zone))
    rows.sort(key=lambda post: post["timestamp_ms"], reverse=True)
    posts = rows[:limit]
    next_before = posts[-1]["timestamp_ms"] if len(rows) > limit and not reached else None
    return {
        "posts": posts,
        "next_before": next_before,
        "collections": {"id": collection_id, "names": names},
    }


def _post(collection_id: str, body: dict) -> dict:
    token = client.load_private("session.bin")["token"]
    path = "chat/" + urllib.parse.quote(collection_id, safe="") + "/message"
    return client.api("POST", path, body, token=token)


def build_text_message(room_item: dict, user: dict, private: bytes, text: str, parent_id: str) -> dict:
    message_id = str(uuid.uuid4())
    sender = user["userId"]
    content_key = os.urandom(32)
    aad = crypto.message_aad(room_item["id"], message_id, sender, TEXT_CONTENT_TYPE, 1)
    combined = crypto.encrypt(content_key, text.encode("utf-8"), aad)
    keys = crypto.wrap_content_key(
        private, content_key, room_item["id"], sender, upload.recipients(room_item, user), 1
    )
    return {
        "type": TEXT_TYPE,
        "conversationId": room_item["id"],
        "messageId": message_id,
        "parentId": parent_id,
        "uuid": str(uuid.uuid4()),
        "refreshesStaleKeys": True,
        "clientTimestamp": int(time.time() * 1000),
        "contentType": TEXT_CONTENT_TYPE,
        "message": "",
        "envelope": {
            "v": 1,
            "alg": "CHACHA20-POLY1305",
            "aadB64": crypto.b64(aad),
            "combinedB64": crypto.b64(combined),
        },
        "keys": keys,
    }


def fetch_message(collection_id: str, message_id: str) -> dict:
    token = client.load_private("session.bin")["token"]
    query = urllib.parse.urlencode({"conversationId": collection_id})
    result = client.api("GET", f"chat/message/{urllib.parse.quote(message_id, safe='')}?{query}", token=token)
    message = result.get("message")
    if not isinstance(message, dict):
        raise client.SetlogError("投稿を読み戻せませんでした。")
    return message


def send_text(collection_id: str, text: str, parent_id: str = "") -> dict:
    """テキスト返信を送信し、読み戻して本文と親を照合する。"""
    body = text.strip()
    if not body:
        raise client.SetlogError("送信する本文を入力してください。")
    if len(body) > MAX_TEXT_LENGTH:
        raise client.SetlogError(f"本文は{MAX_TEXT_LENGTH}文字までにしてください。")
    me = user_id()
    private = identity_private()
    user = profile()
    item = room(collection_id)
    if parent_id:
        parent = fetch_message(collection_id, parent_id)
        if parent.get("conversationId") != collection_id:
            raise client.SetlogError("返信先の投稿が指定した投稿先にありません。")
    message = build_text_message(item, user, private, body, parent_id)
    result = _post(collection_id, message)
    returned = result.get("messageId")
    if not isinstance(returned, str) or not returned:
        raise client.SetlogError("送信した投稿IDを確認できませんでした。再送せず読み戻してください。")
    stored = fetch_message(collection_id, returned)
    if stored.get("conversationId") != collection_id or (stored.get("parentId") or "") != parent_id:
        raise client.SetlogError("読み戻した投稿の投稿先または返信先が一致しません。")
    names = member_names(collection_id)
    summary = summarize(stored, me, private, names, ZoneInfo("Asia/Tokyo"))
    if summary["text"] != body:
        raise client.SetlogError("読み戻した本文が送信した内容と一致しません。")
    return {"message_id": returned, "text": summary["text"], "parent_id": summary["parent_id"]}


def _reaction_path(action: str) -> str:
    return f"chat/message/{action}"


def set_reaction(collection_id: str, message_id: str, text: str, *, remove: bool) -> dict:
    """リアクションを付与または解除し、読み戻して一致を確認する。"""
    emoji = (text or "").strip()
    if not emoji or len(emoji) > 16:
        raise client.SetlogError("リアクションの絵文字を1つ指定してください。")
    token = client.load_private("session.bin")["token"]
    me = user_id()
    body: dict[str, Any] = {
        "conversationId": collection_id,
        "messageId": message_id,
        "text": emoji,
    }
    if not remove:
        # アプリはリアクション自体のIDと倍率を付けて追加する。
        body.update({"id": str(uuid.uuid4()), "factor": 1, "scale": 1})
    client.api("POST", _reaction_path("removeReaction" if remove else "addReaction"), body, token=token)
    stored = fetch_message(collection_id, message_id)
    mine = [item for item in reactions(stored) if item.get("user_id") == me and item.get("text") == emoji]
    if remove and mine:
        raise client.SetlogError("リアクションの解除を読み戻しで確認できませんでした。")
    if not remove and not mine:
        raise client.SetlogError("リアクションの付与を読み戻しで確認できませんでした。")
    return {"message_id": message_id, "text": emoji, "removed": remove, "reactions": reactions(stored)}


def delete_message(collection_id: str, message_id: str) -> dict:
    """自分が送った投稿を削除する（取り消し不可）。"""
    token = client.load_private("session.bin")["token"]
    stored = fetch_message(collection_id, message_id)
    if stored.get("senderId") != user_id():
        raise client.SetlogError("自分が送った投稿だけを削除できます。")
    client.api(
        "POST",
        "chat/message/delete",
        {"messageId": message_id, "conversationId": collection_id},
        token=token,
    )
    return {"message_id": message_id, "deleted": True}
