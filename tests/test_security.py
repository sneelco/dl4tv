from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import security
from app.main import create_app
from app.models import AppConfig, SecurityConfig

PASSPHRASE = "correct horse battery staple"


@pytest.fixture(autouse=True)
def clear_throttle():
    api_module._failures.clear()
    yield
    api_module._failures.clear()


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setattr(
        api_module.gauth,
        "auth_status",
        lambda store, base=None: {
            "connected": False,
            "source": "auto",
            "effective_source": "yt-dlp",
            "channel": None,
            "has_client": False,
            "has_api_key": False,
            "token_present": False,
            "redirect_uri": "http://testserver/auth/callback",
        },
    )
    with TestClient(create_app()) as test_client:
        yield test_client


# -- hashing ---------------------------------------------------------------


def test_hash_and_verify():
    stored = security.hash_passphrase(PASSPHRASE)

    assert PASSPHRASE not in stored, "the passphrase itself must never be stored"
    assert stored.startswith("scrypt$")
    assert security.verify_passphrase(PASSPHRASE, stored)
    assert not security.verify_passphrase("wrong", stored)
    assert not security.verify_passphrase("", stored)
    assert not security.verify_passphrase(PASSPHRASE, None)


def test_hashes_are_salted():
    assert security.hash_passphrase(PASSPHRASE) != security.hash_passphrase(PASSPHRASE)


def test_corrupt_hash_denies_rather_than_raising():
    assert not security.verify_passphrase(PASSPHRASE, "garbage")
    assert not security.verify_passphrase(PASSPHRASE, "scrypt$not$a$valid$hash$here")


# -- session tokens --------------------------------------------------------


def test_token_roundtrip():
    secret = b"0" * 32
    token = security.issue_token(secret, "fingerprint")

    assert security.verify_token(token, secret, "fingerprint")


def test_token_rejects_tampering_and_wrong_key():
    secret = b"0" * 32
    token = security.issue_token(secret, "fingerprint")

    assert not security.verify_token(token + "x", secret, "fingerprint")
    assert not security.verify_token(token, b"1" * 32, "fingerprint")
    assert not security.verify_token("nonsense", secret, "fingerprint")
    assert not security.verify_token(None, secret, "fingerprint")


def test_token_expires():
    secret = b"0" * 32
    token = security.issue_token(secret, "fingerprint", ttl=-1)

    assert not security.verify_token(token, secret, "fingerprint")


def test_changing_the_passphrase_invalidates_old_sessions():
    secret = b"0" * 32
    token = security.issue_token(secret, "old-fingerprint")

    assert not security.verify_token(token, secret, "new-fingerprint")


def test_fingerprint_follows_the_passphrase(env):
    config = AppConfig()
    first = security.credential_fingerprint(config, env)

    config.security = SecurityConfig(passphrase_hash=security.hash_passphrase(PASSPHRASE))
    second = security.credential_fingerprint(config, env)

    assert first != second


def test_session_secret_is_stable_and_private(store, env):
    first = store.session_secret()

    assert len(first) >= 32
    assert first == store.session_secret()
    assert env.session_key_file.stat().st_mode & 0o077 == 0, "must not be group/world readable"


# -- the gate --------------------------------------------------------------


def test_open_by_default(client):
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/access").json() == {
        "locked": False,
        "authenticated": True,
        "managed_by_env": False,
    }


def test_setting_a_passphrase_locks_the_api(client):
    response = client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    assert response.status_code == 200
    assert response.json() == {"locked": True}

    # The person who set it keeps their session.
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/access").json()["locked"] is True

    # A different browser is locked out.
    client.cookies.clear()
    assert client.get("/api/status").status_code == 401


def test_unlock_issues_a_working_session(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    client.cookies.clear()

    assert client.post("/api/access/unlock", json={"passphrase": "nope"}).status_code == 401
    assert client.get("/api/status").status_code == 401

    assert client.post("/api/access/unlock", json={"passphrase": PASSPHRASE}).status_code == 200
    assert client.cookies.get(security.SESSION_COOKIE)
    assert client.get("/api/status").status_code == 200


def test_basic_auth_works_for_scripts(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    client.cookies.clear()

    assert client.get("/api/status", auth=("", PASSPHRASE)).status_code == 200
    assert client.get("/api/status", auth=("", "wrong")).status_code == 401


def test_locking_clears_the_session(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})

    client.post("/api/access/lock")

    assert client.get("/api/status").status_code == 401


def test_removing_the_passphrase_reopens_everything(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})

    assert client.put("/api/access/passphrase", json={"passphrase": ""}).json() == {
        "locked": False
    }
    client.cookies.clear()
    assert client.get("/api/status").status_code == 200


def test_changing_the_passphrase_logs_other_browsers_out(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    stale = dict(client.cookies)

    client.put("/api/access/passphrase", json={"passphrase": "a different passphrase"})

    client.cookies.clear()
    client.cookies.update(stale)
    assert client.get("/api/status").status_code == 401


def test_short_passphrases_are_refused(client):
    response = client.put("/api/access/passphrase", json={"passphrase": "short"})

    assert response.status_code == 400
    assert client.get("/api/access").json()["locked"] is False


def test_health_and_login_stay_reachable_when_locked(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    client.cookies.clear()

    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/api/access").json() == {
        "locked": True,
        "authenticated": False,
        "managed_by_env": False,
    }


def test_browser_navigation_is_redirected_to_the_login_page(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    client.cookies.clear()

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_repeated_failures_are_throttled(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    client.cookies.clear()

    for _ in range(api_module._MAX_FAILURES):
        assert client.post("/api/access/unlock", json={"passphrase": "no"}).status_code == 401

    blocked = client.post("/api/access/unlock", json={"passphrase": PASSPHRASE})
    assert blocked.status_code == 429


def test_a_successful_unlock_clears_the_failure_count(client):
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    client.cookies.clear()

    client.post("/api/access/unlock", json={"passphrase": "no"})
    client.post("/api/access/unlock", json={"passphrase": PASSPHRASE})

    assert api_module._failures == {}


# -- environment-managed passphrase ---------------------------------------


@pytest.fixture
def env_locked_client(env, monkeypatch, client):
    from app import settings as settings_module

    monkeypatch.setenv("DL4TV_PASSPHRASE", "from-the-environment")
    settings_module.env.cache_clear()
    yield client
    settings_module.env.cache_clear()


def test_env_passphrase_locks_and_cannot_be_changed_from_the_ui(env_locked_client):
    client = env_locked_client
    client.cookies.clear()

    assert client.get("/api/status").status_code == 401
    assert client.get("/api/access").json() == {
        "locked": True,
        "authenticated": False,
        "managed_by_env": True,
    }

    assert client.post(
        "/api/access/unlock", json={"passphrase": "from-the-environment"}
    ).status_code == 200
    assert client.get("/api/status").status_code == 200

    refused = client.put("/api/access/passphrase", json={"passphrase": "something else"})
    assert refused.status_code == 409
    assert client.get("/api/status").status_code == 200


def test_unlock_is_cheap_enough_to_be_usable(client):
    """scrypt should cost tens of milliseconds, not seconds."""
    client.put("/api/access/passphrase", json={"passphrase": PASSPHRASE})
    client.cookies.clear()

    start = time.monotonic()
    client.post("/api/access/unlock", json={"passphrase": PASSPHRASE})
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"unlock took {elapsed:.2f}s"
