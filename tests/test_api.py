from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app.main import create_app


@pytest.fixture
def client(env, monkeypatch):
    # Never let the tests reach out to YouTube.
    monkeypatch.setattr(
        api_module.gauth,
        "auth_status",
        lambda store, base=None: {
            "connected": False,
            "channel": None,
            "has_client": False,
            "has_api_key": False,
            "token_present": False,
            "redirect_uri": "http://testserver/auth/callback",
        },
    )
    with TestClient(create_app()) as test_client:
        yield test_client


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "dl4tv" in response.text


def test_status_starts_empty(client):
    body = client.get("/api/status").json()

    assert body["running"] is False
    assert body["playlists"] == []
    assert body["schedule"]["mode"] == "daily"


def test_mapping_crud(client, env):
    created = client.post(
        "/api/mappings",
        json={"playlist_id": "PLabc", "title": "Cooking", "folder": "cooking"},
    )
    assert created.status_code == 201
    mapping_id = created.json()["id"]

    listed = client.get("/api/mappings").json()
    assert [m["playlist_id"] for m in listed["mappings"]] == ["PLabc"]
    assert listed["download_dir"] == str(env.download_dir)

    duplicate = client.post(
        "/api/mappings", json={"playlist_id": "PLabc", "folder": "other"}
    )
    assert duplicate.status_code == 409

    patched = client.patch(
        f"/api/mappings/{mapping_id}",
        json={"folder": "shows/cooking", "min_duration_seconds": 61},
    )
    assert patched.status_code == 200
    assert patched.json()["folder"] == "shows/cooking"
    assert patched.json()["min_duration_seconds"] == 61

    status = client.get("/api/status").json()
    assert status["playlists"][0]["resolved_folder"] == str(env.download_dir / "shows/cooking")

    assert client.delete(f"/api/mappings/{mapping_id}").status_code == 200
    assert client.get("/api/mappings").json()["mappings"] == []


def test_mapping_requires_a_playlist_reference(client):
    response = client.post("/api/mappings", json={"folder": "somewhere"})
    assert response.status_code == 400


def test_unknown_mapping_is_404(client):
    assert client.get("/api/mappings/nope/videos").status_code == 404
    assert client.patch("/api/mappings/nope", json={"folder": "x"}).status_code == 404
    assert client.post("/api/mappings/nope/sync").status_code == 404


def test_settings_roundtrip_and_secret_masking(client):
    response = client.put(
        "/api/settings",
        json={
            "youtube": {"client_id": "cid", "client_secret": "shhh", "api_key": "key"},
            "schedule": {"enabled": True, "mode": "interval", "interval_minutes": 30},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["youtube"]["client_id"] == "cid"
    assert body["youtube"]["client_secret"] == api_module.MASK
    assert body["schedule"]["interval_minutes"] == 30

    # Sending the mask back must not overwrite the stored secret.
    client.put(
        "/api/settings",
        json={"youtube": {"client_id": "cid", "client_secret": api_module.MASK}},
    )
    from app.store import get_store

    assert get_store().config.youtube.client_secret == "shhh"


def test_folder_listing_and_creation(client, env):
    (env.download_dir / "existing").mkdir()

    listing = client.get("/api/folders").json()
    assert listing["root"] == str(env.download_dir)
    assert [f["name"] for f in listing["folders"]] == ["existing"]

    created = client.post("/api/folders", json={"path": "new/nested"})
    assert created.status_code == 201
    assert (env.download_dir / "new" / "nested").is_dir()


def test_folder_traversal_is_rejected(client):
    assert client.get("/api/folders", params={"path": "../../etc"}).status_code == 400
    assert client.post("/api/folders", json={"path": "../escape"}).status_code == 400


def test_video_retry_and_forget(client):
    mapping_id = client.post(
        "/api/mappings", json={"playlist_id": "PLx", "folder": "f"}
    ).json()["id"]

    from app.models import VideoRecord
    from app.store import get_store

    store = get_store()
    store.update_state(
        lambda state: state.playlist(mapping_id).videos.__setitem__(
            "v1",
            VideoRecord(video_id="v1", status="failed", permanent=True, error_kind="drm", attempts=3),
        )
    )

    assert client.post(f"/api/mappings/{mapping_id}/videos/v1/retry").status_code == 200
    record = store.state.playlist(mapping_id).videos["v1"]
    assert record.permanent is False and record.attempts == 0

    assert client.delete(f"/api/mappings/{mapping_id}/videos/v1").status_code == 200
    assert store.state.playlist(mapping_id).videos == {}


def test_playlists_endpoint_reports_missing_credentials(client):
    # With no credentials the yt-dlp source takes over, and it cannot enumerate
    # an account's own playlists -- only public ones added by URL.
    response = client.get("/api/youtube/playlists")
    assert response.status_code == 401
    assert "google account" in response.json()["detail"].lower()


def test_public_playlist_can_be_added_without_any_credentials(client, monkeypatch):
    """The whole point of the yt-dlp source: no API key, no OAuth, still works."""
    from app.ytdlp import YtDlpSource

    def fake_extract(url, limit=None):
        assert "list=PLpublic" in url
        return {
            "title": "Public Cooking",
            "playlist_count": 3,
            "entries": [{"id": "a", "title": "Video a", "duration": 600}],
        }

    monkeypatch.setattr(
        api_module, "make_source", lambda store: YtDlpSource(extractor=fake_extract)
    )

    created = client.post(
        "/api/mappings",
        json={
            "query": "https://www.youtube.com/playlist?list=PLpublic",
            "folder": "cooking",
        },
    )

    assert created.status_code == 201
    assert created.json()["playlist_id"] == "PLpublic"
    assert created.json()["title"] == "Public Cooking"


def test_settings_expose_the_playlist_source(client):
    assert client.get("/api/settings").json()["youtube"]["source"] == "auto"

    response = client.put("/api/settings", json={"youtube": {"source": "yt-dlp"}})

    assert response.json()["youtube"]["source"] == "yt-dlp"
    from app.store import get_store

    assert get_store().config.youtube.source == "yt-dlp"


def test_logs_endpoint(client):
    import logging

    logging.getLogger("dl4tv.test").info("hello from the test")
    logs = client.get("/api/logs").json()["logs"]
    assert any("hello from the test" in entry["message"] for entry in logs)
