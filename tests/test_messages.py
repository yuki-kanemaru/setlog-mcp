"""チャット（暗号化テキスト）・リアクション・要約の単体テスト。"""

from __future__ import annotations

import time
from zoneinfo import ZoneInfo

import pytest

from setlog_mcp import client, crypto, messages

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
ROOM = {
    "id": "33333333-3333-3333-3333-333333333333",
    "name": "テスト",
    "participants": [{"userId": OTHER, "name": "あいて"}],
}


def _keys():
    private, public = crypto.generate_pair()
    other_private, other_public = crypto.generate_pair()
    user = {
        "userId": ME,
        "name": "じぶん",
        "kid": crypto.key_id(public),
        "publicKey": crypto.b64(public),
    }
    room = {
        **ROOM,
        "participants": [
            {
                "userId": OTHER,
                "name": "あいて",
                "publicKey": crypto.b64(other_public),
                "kid": crypto.key_id(other_public),
            }
        ],
    }
    return private, other_private, user, room


def test_build_text_message_roundtrip():
    private, _other_private, user, room = _keys()
    body = messages.build_text_message(room, user, private, "こんにちは", "")
    body["senderId"] = ME
    assert body["type"] == "chat"
    assert body["contentType"] == "text/plain"
    assert body["message"] == ""
    assert body["parentId"] == ""
    assert len(body["keys"]) == 2
    assert body["envelope"]["v"] == 1

    plain = messages.decrypt_text(body, private, ME)
    assert plain == "こんにちは"


def test_build_text_message_sets_parent():
    private, _other_private, user, room = _keys()
    body = messages.build_text_message(room, user, private, "返信", "abc")
    assert body["parentId"] == "abc"


def test_decrypt_text_rejects_missing_envelope():
    private, _other_private, user, room = _keys()
    body = messages.build_text_message(room, user, private, "本文", "")
    body.pop("envelope")
    assert messages.decrypt_text(body, private, ME) == ""


def test_reactions_ignores_unknown_entries():
    message = {
        "reactions": [
            {"text": "✨", "userId": ME, "timestamp": 1},
            {"text": "", "userId": OTHER},
            "invalid",
            {"userId": OTHER},
        ]
    }
    parsed = messages.reactions(message)
    assert parsed == [{"text": "✨", "user_id": ME, "timestamp": 1}]


def test_member_names_merges_participants_and_profiles(monkeypatch):
    monkeypatch.setattr(
        messages,
        "room",
        lambda _identifier: {
            "participants": [{"userId": OTHER, "name": "あいて"}],
            "memberProfiles": {ME: {"name": "じぶん"}},
        },
    )
    names = messages.member_names(ROOM["id"])
    assert names == {OTHER: "あいて", ME: "じぶん"}


def test_summarize_collects_media_and_text(monkeypatch):
    private, _other_private, user, room = _keys()
    body = messages.build_text_message(room, user, private, "やあ", "")
    body["senderId"] = ME
    body["timestamp"] = int(time.time() * 1000)
    summary = messages.summarize(body, ME, private, {ME: "じぶん"}, ZoneInfo("Asia/Tokyo"))
    assert summary["kind"] == "chat"
    assert summary["text"] == "やあ"
    assert summary["is_mine"] is True
    assert summary["media"] == []


def test_summarize_marks_unread_when_not_in_read_by():
    message = {
        "messageId": "a",
        "conversationId": ROOM["id"],
        "senderId": OTHER,
        "timestamp": 1000,
        "contentType": "video/quicktime",
        "keys": [],
        "media": {"path": "p", "mimeType": "video/mp4", "permission": "allow_saving"},
        "readBy": [],
    }
    summary = messages.summarize(message, ME, b"", {OTHER: "あいて"}, ZoneInfo("Asia/Tokyo"))
    assert summary["kind"] == "media"
    assert summary["media"][0]["permission"] == "allow_saving"


def test_read_feed_skips_deleted_and_stops_at_limit(monkeypatch):
    now = int(time.time() * 1000)
    rows = [
        {
            "messageId": "1",
            "conversationId": ROOM["id"],
            "senderId": OTHER,
            "timestamp": now,
            "contentType": "text/plain",
        },
        {
            "messageId": "2",
            "conversationId": ROOM["id"],
            "senderId": OTHER,
            "timestamp": now - 10,
            "isDeleted": True,
        },
        {
            "messageId": "3",
            "conversationId": ROOM["id"],
            "senderId": OTHER,
            "timestamp": now - 20,
            "contentType": "text/plain",
        },
    ]
    monkeypatch.setattr(messages, "iter_messages", lambda *args, **kwargs: iter(rows))
    monkeypatch.setattr(messages, "member_names", lambda _identifier: {})
    monkeypatch.setattr(messages, "user_id", lambda: ME)
    monkeypatch.setattr(messages, "identity_private", lambda: b"")
    monkeypatch.setattr(client, "load_private", lambda name: {"token": "t"})
    feed = messages.read_feed(ROOM["id"], limit=1)
    assert [post["message_id"] for post in feed["posts"]] == ["1"]


def test_unwrap_key_requires_own_entry():
    private, _other_private, user, room = _keys()
    body = messages.build_text_message(room, user, private, "x", "")
    with pytest.raises(client.SetlogError, match="自分宛ての鍵"):
        messages.unwrap_key(body, private, "99999999-9999-9999-9999-999999999999")


def test_read_feed_returns_newest_first(monkeypatch):
    """一覧APIは1ページ内を古い順で返すため、Web向けには新しい順へ並べ替える。"""
    now = int(time.time() * 1000)
    rows = [
        {
            "messageId": "old",
            "conversationId": ROOM["id"],
            "senderId": OTHER,
            "timestamp": now - 2000,
            "contentType": "text/plain",
        },
        {
            "messageId": "mid",
            "conversationId": ROOM["id"],
            "senderId": OTHER,
            "timestamp": now - 1000,
            "contentType": "text/plain",
        },
        {
            "messageId": "new",
            "conversationId": ROOM["id"],
            "senderId": OTHER,
            "timestamp": now,
            "contentType": "text/plain",
        },
    ]
    monkeypatch.setattr(messages, "iter_messages", lambda *args, **kwargs: iter(rows))
    monkeypatch.setattr(messages, "member_names", lambda _identifier: {})
    monkeypatch.setattr(messages, "user_id", lambda: ME)
    monkeypatch.setattr(messages, "identity_private", lambda: b"")
    monkeypatch.setattr(client, "load_private", lambda name: {"token": "t"})

    feed = messages.read_feed(ROOM["id"], limit=2)
    assert [post["message_id"] for post in feed["posts"]] == ["new", "mid"]
    assert feed["next_before"] == now - 1000


def test_read_feed_tolerates_slightly_future_stamps(monkeypatch):
    """端末時計が遅れていても、直前に届いた投稿を取りこぼさない。"""
    now = int(time.time() * 1000)
    rows = [
        {
            "messageId": "future",
            "conversationId": ROOM["id"],
            "senderId": OTHER,
            "timestamp": now + 60_000,
            "contentType": "text/plain",
        },
    ]
    monkeypatch.setattr(messages, "iter_messages", lambda *args, **kwargs: iter(rows))
    monkeypatch.setattr(messages, "member_names", lambda _identifier: {})
    monkeypatch.setattr(messages, "user_id", lambda: ME)
    monkeypatch.setattr(messages, "identity_private", lambda: b"")
    monkeypatch.setattr(client, "load_private", lambda name: {"token": "t"})
    feed = messages.read_feed(ROOM["id"], limit=5)
    assert [post["message_id"] for post in feed["posts"]] == ["future"]


def test_set_reaction_posts_identifier_then_verifies(monkeypatch):
    """追加時はリアクションIDと倍率を送り、読み戻しで自分の分を確認する。"""
    captured = {}

    def fake_api(method, path, body=None, token=None, base=None):
        captured.setdefault("calls", []).append((method, path, body))
        return {}

    stored = {"messageId": "m1", "reactions": [{"text": "✨", "userId": ME, "timestamp": 1}]}
    monkeypatch.setattr(client, "api", fake_api)
    monkeypatch.setattr(client, "load_private", lambda name: {"token": "t", "user": {"userId": ME}})
    monkeypatch.setattr(messages, "user_id", lambda: ME)
    monkeypatch.setattr(messages, "fetch_message", lambda collection, message: stored)

    result = messages.set_reaction(ROOM["id"], "m1", "✨", remove=False)
    body = captured["calls"][0][2]
    assert captured["calls"][0][1] == "chat/message/addReaction"
    assert body["conversationId"] == ROOM["id"]
    assert body["messageId"] == "m1"
    assert body["text"] == "✨"
    assert len(body["id"]) == 36
    assert body["factor"] == 1
    assert result["removed"] is False
    assert result["reactions"][0]["user_id"] == ME


def test_remove_reaction_omits_identifier(monkeypatch):
    captured = {}

    def fake_api(method, path, body=None, token=None, base=None):
        captured["call"] = (method, path, body)
        return {}

    monkeypatch.setattr(client, "api", fake_api)
    monkeypatch.setattr(client, "load_private", lambda name: {"token": "t", "user": {"userId": ME}})
    monkeypatch.setattr(messages, "user_id", lambda: ME)
    monkeypatch.setattr(
        messages, "fetch_message", lambda collection, message: {"messageId": "m1", "reactions": []}
    )

    result = messages.set_reaction(ROOM["id"], "m1", "✨", remove=True)
    method, path, body = captured["call"]
    assert path == "chat/message/removeReaction"
    assert "id" not in body
    assert result["removed"] is True
