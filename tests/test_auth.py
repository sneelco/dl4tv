"""OAuth flow tests.

The PKCE half of this matters: sign-in spans two HTTP requests, and the code
verifier generated for the first must survive to the second or Google rejects
the exchange with "Missing code verifier".
"""

from __future__ import annotations

import pytest

from app import auth
from app.models import AppConfig, YouTubeConfig
from app.youtube import NotAuthenticated

REDIRECT = "http://localhost:8484/auth/callback"


@pytest.fixture(autouse=True)
def clear_pending():
    auth._PENDING.clear()
    yield
    auth._PENDING.clear()


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(youtube=YouTubeConfig(client_id="cid", client_secret="sec"))


class FakeFlow:
    """Captures what the real Flow would have been handed."""

    instances: list[FakeFlow] = []

    def __init__(self) -> None:
        self.code_verifier = None
        self.redirect_uri = None
        self.fetched_with = None
        self.credentials = type("C", (), {"to_json": lambda self: '{"token": "abc"}'})()
        FakeFlow.instances.append(self)

    def fetch_token(self, code=None, **kwargs):
        self.fetched_with = code


def test_authorization_url_includes_pkce_challenge(config):
    url, state = auth.authorization_url(config, REDIRECT)

    assert "code_challenge=" in url
    assert f"state={state}" in url
    assert "access_type=offline" in url
    # The verifier is held for the callback that follows.
    assert auth._PENDING[state]["code_verifier"]
    assert auth._PENDING[state]["redirect_uri"] == REDIRECT


def test_exchange_uses_the_verifier_from_the_authorization_request(config, store, monkeypatch):
    _url, state = auth.authorization_url(config, REDIRECT)
    expected = auth._PENDING[state]["code_verifier"]

    FakeFlow.instances.clear()
    monkeypatch.setattr(auth, "build_flow", lambda config, uri: FakeFlow())

    auth.exchange_code(store, config, "the-code", state)

    flow = FakeFlow.instances[-1]
    assert flow.code_verifier == expected, "verifier must carry across the two requests"
    assert flow.fetched_with == "the-code"
    assert store.read_token() == {"token": "abc"}


def test_exchange_without_state_is_rejected(config, store):
    with pytest.raises(NotAuthenticated, match="Connect again"):
        auth.exchange_code(store, config, "the-code", None)


def test_exchange_with_an_unknown_state_is_rejected(config, store):
    auth.authorization_url(config, REDIRECT)

    with pytest.raises(NotAuthenticated, match="Connect again"):
        auth.exchange_code(store, config, "the-code", "not-a-state-we-issued")


def test_a_pending_sign_in_is_single_use(config, store, monkeypatch):
    _url, state = auth.authorization_url(config, REDIRECT)
    monkeypatch.setattr(auth, "build_flow", lambda config, uri: FakeFlow())

    auth.exchange_code(store, config, "the-code", state)

    # Replaying the same callback must not work.
    with pytest.raises(NotAuthenticated):
        auth.exchange_code(store, config, "the-code", state)


def test_expired_pending_sign_ins_are_rejected(config, store, monkeypatch):
    _url, state = auth.authorization_url(config, REDIRECT)
    auth._PENDING[state]["created_at"] -= auth._PENDING_TTL_SECONDS + 1

    with pytest.raises(NotAuthenticated, match="expired"):
        auth.exchange_code(store, config, "the-code", state)


def test_pending_sign_ins_are_capped(config):
    states = [auth.authorization_url(config, REDIRECT)[1] for _ in range(auth._PENDING_MAX + 3)]

    assert len(auth._PENDING) <= auth._PENDING_MAX
    # The most recent attempt always survives.
    assert states[-1] in auth._PENDING


def test_flow_requires_a_client(store):
    with pytest.raises(NotAuthenticated, match="No OAuth client"):
        auth.authorization_url(AppConfig(), REDIRECT)


def test_env_public_url_overrides_the_request_host(config, monkeypatch):
    from app import settings as settings_module

    monkeypatch.setenv("DL4TV_PUBLIC_URL", "https://dl4tv.example.com/")
    settings_module.env.cache_clear()
    try:
        assert (
            auth.redirect_uri(config, "http://ignored/")
            == "https://dl4tv.example.com/auth/callback"
        )
    finally:
        settings_module.env.cache_clear()


# -- public URL ------------------------------------------------------------


def test_public_url_prefers_config_over_the_request_host(config):
    """Behind a proxy the request's own host is not the address you browse to."""
    config.public_url = "https://dl4tv.example.com"

    assert (
        auth.redirect_uri(config, "http://10.42.0.7:8484/")
        == "https://dl4tv.example.com/auth/callback"
    )


def test_public_url_falls_back_to_the_request_host(config):
    assert (
        auth.redirect_uri(config, "http://localhost:8484/")
        == "http://localhost:8484/auth/callback"
    )


def test_env_public_url_wins_over_the_configured_one(config, monkeypatch):
    from app import settings as settings_module

    config.public_url = "https://from-settings.example.com"
    monkeypatch.setenv("DL4TV_PUBLIC_URL", "https://from-env.example.com")
    settings_module.env.cache_clear()
    try:
        assert (
            auth.redirect_uri(config, "http://localhost:8484/")
            == "https://from-env.example.com/auth/callback"
        )
    finally:
        settings_module.env.cache_clear()


def test_trailing_slashes_never_double_up(config):
    config.public_url = "https://dl4tv.example.com/"

    assert auth.redirect_uri(config) == "https://dl4tv.example.com/auth/callback"


def test_the_authorization_url_uses_the_public_redirect(config, store):
    config.public_url = "https://dl4tv.example.com"
    uri = auth.redirect_uri(config)

    url, state = auth.authorization_url(config, uri)

    assert "redirect_uri=https%3A%2F%2Fdl4tv.example.com%2Fauth%2Fcallback" in url
    # The exchange must reuse exactly the same redirect URI.
    assert auth._PENDING[state]["redirect_uri"] == "https://dl4tv.example.com/auth/callback"
