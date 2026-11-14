"""ログ動画の保存処理（鍵の展開・復号・保存条件）を検証する。"""

import json
import os

import pytest

from setlog_mcp import client, crypto, media


def _message(sender_private, sender_id, me, recipient_public, collection, message_id, payload, version=2):
    content_key = os.urandom(32)
    wrapped = crypto.wrap_content_key(
        sender_private,
        content_key,
        collection,
        sender_id,
        [{"userId": me, "publicKey": crypto.b64(recipient_public)}],
        2,
    )
    aad = crypto.media_aad(collection, message_id, sender_id, "video/mp4", version)
    blob = crypto.encrypt(content_key, payload, aad)
    message = {
        "conversationId": collection,
        "messageId": message_id,
        "senderId": sender_id,
        "senderName": "sender",
        "contentType": "video/mp4",
        "mediaCipherV": version,
        "keys": wrapped,
        "media": {"path": "ap/uploads/x.mov", "region": "ap-northeast-2", "permission": "allow_saving"},
        "timestamp": 1763000000000,
    }
    return message, blob


def test_safe_name_removes_path_characters():
    assert media._safe_name('a/b:c*?"<>|') == "a_b_c______"
    assert media._safe_name("   ") == "unknown"
    assert len(media._safe_name("x" * 100)) == 40


def test_unwrap_and_decrypt_roundtrip():
    sender_private, sender_public = crypto.generate_pair()
    me_private, me_public = crypto.generate_pair()
    payload = b"\x00\x00\x00\x18ftypmp42" + os.urandom(64)
    message, blob = _message(
        sender_private, "sender-id", "me-id", me_public, "collection-id", "message-id", payload
    )
    content_key = media._unwrap_content_key(message, me_private, "me-id")
    assert len(content_key) == 32
    assert media._decrypt_media(message, content_key, blob) == payload


def test_decrypt_rejects_other_recipient():
    sender_private, _ = crypto.generate_pair()
    _, other_public = crypto.generate_pair()
    me_private, _ = crypto.generate_pair()
    message, blob = _message(
        sender_private, "sender-id", "other-id", other_public, "collection-id", "message-id", b"data"
    )
    with pytest.raises(client.SetlogError):
        media._unwrap_content_key(message, me_private, "me-id")


def test_save_log_media_writes_file_and_skips_forbidden(tmp_path, monkeypatch):
    sender_private, _ = crypto.generate_pair()
    me_private, me_public = crypto.generate_pair()
    payload = b"\x00\x00\x00\x18ftypmp42" + os.urandom(32)
    allowed, blob = _message(
        sender_private, "sender-id", "me-id", me_public, "collection-id", "message-1", payload
    )
    forbidden, _ = _message(
        sender_private, "sender-id", "me-id", me_public, "collection-id", "message-2", payload
    )
    forbidden["media"]["permission"] = "deny_saving"

    monkeypatch.setattr(client, "load_private", lambda name: {
        "session.bin": {"token": "token"},
        "identity.bin": {"userId": "me-id", "identityPrivateKeyB64": crypto.b64(me_private)},
    }[name])

    def fake_api(method, path, body=None, token=None, *, base=client.BASE):
        if path == "user/profile":
            return {"user": {"userId": "me-id"}}
        if path.startswith("chat/"):
            return {"messages": [allowed, forbidden], "hasMore": False}
        raise AssertionError(path)

    monkeypatch.setattr(client, "api", fake_api)
    monkeypatch.setattr(media.upload, "collections", lambda: [{"id": "collection-id", "name": "log"}])
    monkeypatch.setattr(media, "_download", lambda media_item, token: blob)
    monkeypatch.setattr(media, "_remux", lambda source, destination: False)

    result = media.save_log_media("collection-id", output_dir=str(tmp_path))

    assert result["saved_count"] == 1
    assert result["skipped"][0]["reason"].startswith("保存不可")
    saved = result["saved"][0]["file"]
    assert saved.endswith(".mov")
    with open(saved, "rb") as stream:
        assert stream.read() == payload
    assert json.dumps(result, ensure_ascii=False)  # 応答はJSON化できる
