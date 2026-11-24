"""Web層（トークン検証・エラー変換）の結合テスト。"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from setlog_web import app as web_app
from setlog_web import media_cache, service


@pytest.fixture()
def client():
    return TestClient(web_app.app)


def _login(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert web_app.COOKIE in response.cookies or web_app.COOKIE in client.cookies


def test_index_sets_session_cookie(client):
    response = client.get("/")
    assert response.status_code == 200
    assert web_app.TOKEN in response.text
    assert client.cookies.get(web_app.COOKIE) == web_app.TOKEN


def test_api_requires_cookie(client):
    response = client.get("/api/status")
    assert response.status_code == 403
    assert "error" in response.json()


def test_api_rejects_wrong_cookie(client):
    client.cookies.set(web_app.COOKIE, "wrong-value")
    response = client.get("/api/status")
    assert response.status_code == 403


def test_api_status_with_cookie(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(service, "status", lambda: {"authenticated": True, "profile": {"name": "t"}})
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is True


def test_post_requires_token_header(client, monkeypatch):
    _login(client)
    response = client.post("/api/reactions", json={"collection_id": "c", "message_id": "m", "text": "✨"})
    assert response.status_code == 403


def test_reaction_endpoint_validates_body(client, monkeypatch):
    _login(client)
    headers = {"x-setlog-token": web_app.TOKEN}
    response = client.post("/api/reactions", json={"collection_id": "c"}, headers=headers)
    assert response.status_code == 400
    assert "message_id" in response.json()["error"]


def test_reaction_endpoint_returns_result(client, monkeypatch):
    _login(client)
    headers = {"x-setlog-token": web_app.TOKEN}
    monkeypatch.setattr(
        service, "react", lambda collection, message, text, remove: {"text": text, "removed": remove}
    )
    response = client.post(
        "/api/reactions",
        json={"collection_id": "c", "message_id": "m", "text": "✨", "remove": True},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json() == {"text": "✨", "removed": True}


def test_reply_endpoint_returns_result(client, monkeypatch):
    _login(client)
    headers = {"x-setlog-token": web_app.TOKEN}
    captured = {}

    def fake(collection, text, parent):
        captured.update({"collection": collection, "text": text, "parent": parent})
        return {"message_id": "m", "text": text, "parent_id": parent}

    monkeypatch.setattr(service, "reply", fake)
    response = client.post(
        "/api/replies",
        json={"collection_id": "c", "text": "やあ", "parent_id": "p"},
        headers=headers,
    )
    assert response.status_code == 200
    assert captured == {"collection": "c", "text": "やあ", "parent": "p"}


def test_busy_error_maps_to_409(client, monkeypatch):
    _login(client)
    headers = {"x-setlog-token": web_app.TOKEN}

    def busy(*args, **kwargs):
        raise service.Busy("他の処理が実行中です。")

    monkeypatch.setattr(service, "react", busy)
    response = client.post(
        "/api/reactions",
        json={"collection_id": "c", "message_id": "m", "text": "✨"},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["busy"] is True


def test_media_unavailable_maps_to_404(client, monkeypatch):
    _login(client)

    def unavailable(*args, **kwargs):
        raise media_cache.MediaUnavailable("削除された投稿です。")

    monkeypatch.setattr(service, "video_path", unavailable)
    response = client.get("/api/media/c/m")
    assert response.status_code == 404
    assert "削除" in response.json()["error"]


def test_audio_route_serves_audio(client, monkeypatch, tmp_path):
    _login(client)
    sample = tmp_path / "sound.m4a"
    sample.write_bytes(b"\x00" * 32)
    monkeypatch.setattr(service, "audio_path", lambda collection, message: sample)
    response = client.get("/api/media/c/m/audio")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mp4")


def test_unknown_error_is_not_leaked(client, monkeypatch):
    _login(client)
    headers = {"x-setlog-token": web_app.TOKEN}

    def explode(*args, **kwargs):
        raise RuntimeError("secret-token-abc")

    monkeypatch.setattr(service, "react", explode)
    response = client.post(
        "/api/reactions",
        json={"collection_id": "c", "message_id": "m", "text": "✨"},
        headers=headers,
    )
    assert response.status_code == 500
    assert "secret" not in response.text


def test_feed_requires_collection(client):
    _login(client)
    response = client.get("/api/feed")
    assert response.status_code == 400


def test_cache_endpoints(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(media_cache, "stats", lambda: {"files": 0, "bytes": 0, "limit": 1})
    monkeypatch.setattr(media_cache, "clear", lambda: {"removed": 2})
    assert client.get("/api/cache").status_code == 200
    response = client.post("/api/cache/clear", headers={"x-setlog-token": web_app.TOKEN})
    assert response.json()["removed"] == 2
