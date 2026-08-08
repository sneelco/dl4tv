"""Google OAuth handling: build the consent URL, swap the code for a token,
keep the refresh token on disk and hand out fresh access tokens.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from .models import AppConfig
from .settings import env
from .store import Store
from .youtube import SCOPES, NotAuthenticated, YouTubeClient

log = logging.getLogger("dl4tv.auth")


def _lazy_google() -> tuple[Any, Any, Any]:
    from google.auth.transport.requests import Request  # noqa: PLC0415
    from google.oauth2.credentials import Credentials  # noqa: PLC0415
    from google_auth_oauthlib.flow import Flow  # noqa: PLC0415

    return Request, Credentials, Flow


def public_base_url(config: AppConfig, request_base_url: str | None = None) -> str:
    """dl4tv's address as a browser sees it.

    An environment variable wins so a deployment can pin it; then the value set
    in Settings; and only failing both, the host this request arrived on --
    which is wrong behind a reverse proxy or a Kubernetes ingress.
    """
    for candidate in (env().public_url, config.public_url, request_base_url):
        if candidate:
            return candidate.strip().rstrip("/")
    return ""


def redirect_uri(config: AppConfig, request_base_url: str | None = None) -> str:
    """Where Google sends the browser back to after consent."""
    return f"{public_base_url(config, request_base_url)}/auth/callback"


def _client_config(config: AppConfig, uri: str) -> dict:
    return {
        "web": {
            "client_id": config.youtube.client_id,
            "client_secret": config.youtube.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [uri],
        }
    }


def build_flow(config: AppConfig, uri: str):
    if not (config.youtube.client_id and config.youtube.client_secret):
        raise NotAuthenticated(
            "No OAuth client configured. Add a client id and secret in Settings first."
        )
    if env().insecure_oauth_transport:
        # Self-hosted installs are usually plain http on a LAN.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    _, _, Flow = _lazy_google()
    flow = Flow.from_client_config(_client_config(config, uri), scopes=SCOPES)
    flow.redirect_uri = uri
    return flow


# Sign-in spans two requests: /auth/start builds the consent URL, and Google
# sends the browser back to /auth/callback. PKCE means the verifier generated
# for the first request has to be presented on the second one, so hold onto it
# in between, keyed by the OAuth state parameter.
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()
_PENDING_TTL_SECONDS = 900
_PENDING_MAX = 8


def _remember_flow(state: str, code_verifier: str | None, uri: str) -> None:
    with _PENDING_LOCK:
        now = time.monotonic()
        for key, pending in list(_PENDING.items()):
            if now - pending["created_at"] > _PENDING_TTL_SECONDS:
                del _PENDING[key]
        while len(_PENDING) >= _PENDING_MAX:
            oldest = min(_PENDING, key=lambda k: _PENDING[k]["created_at"])
            del _PENDING[oldest]
        _PENDING[state] = {
            "code_verifier": code_verifier,
            "redirect_uri": uri,
            "created_at": now,
        }


def _take_flow(state: str | None) -> dict | None:
    """Pop a pending sign-in. Single use, so a code cannot be replayed."""
    if not state:
        return None
    with _PENDING_LOCK:
        pending = _PENDING.pop(state, None)
    if pending is None:
        return None
    if time.monotonic() - pending["created_at"] > _PENDING_TTL_SECONDS:
        return None
    return pending


def authorization_url(config: AppConfig, uri: str) -> tuple[str, str]:
    flow = build_flow(config, uri)
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        # Force a refresh token even when the account has consented before.
        prompt="consent",
    )
    _remember_flow(state, flow.code_verifier, uri)
    return url, state


def exchange_code(store: Store, config: AppConfig, code: str, state: str | None) -> None:
    pending = _take_flow(state)
    if pending is None:
        raise NotAuthenticated(
            "This sign-in could not be matched to a request from this app. It may "
            "have expired, dl4tv may have restarted, or the link was already used. "
            "Click Connect again to start over."
        )
    flow = build_flow(config, pending["redirect_uri"])
    # Without the verifier from /auth/start, Google rejects the exchange with
    # "Missing code verifier".
    flow.code_verifier = pending["code_verifier"]
    flow.fetch_token(code=code)
    store.write_token(flow.credentials.to_json())
    log.info("stored new YouTube OAuth token")


def load_credentials(store: Store):
    """Return valid credentials, refreshing and re-saving them if needed."""
    payload = store.read_token()
    if not payload:
        return None
    Request, Credentials, _ = _lazy_google()
    config = store.config
    # The client id/secret are needed for refresh; prefer the stored copy but
    # fall back to config so rotating the secret does not orphan the token.
    payload = dict(payload)
    payload.setdefault("client_id", config.youtube.client_id)
    payload.setdefault("client_secret", config.youtube.client_secret)
    try:
        credentials = Credentials.from_authorized_user_info(payload, SCOPES)
    except Exception as exc:  # noqa: BLE001
        log.error("stored token is unusable: %s", exc)
        return None
    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            store.write_token(credentials.to_json())
            return credentials
        except Exception as exc:  # noqa: BLE001
            log.error("token refresh failed: %s", exc)
            return None
    return None


def access_token(store: Store) -> str | None:
    credentials = load_credentials(store)
    return credentials.token if credentials else None


def auth_status(store: Store, request_base_url: str | None = None) -> dict:
    config = store.config
    credentials = load_credentials(store)
    connected = credentials is not None
    channel = None
    if connected:
        try:
            with YouTubeClient(access_token=credentials.token) as client:
                payload = client._get("channels", {"part": "snippet", "mine": "true"})
                items = payload.get("items", [])
                if items:
                    channel = items[0].get("snippet", {}).get("title")
        except Exception as exc:  # noqa: BLE001 - status must never 500
            log.debug("could not read channel name: %s", exc)
    from .sources import effective_source  # noqa: PLC0415 - avoids a cycle

    return {
        "connected": connected,
        "source": config.youtube.source,
        "effective_source": effective_source(config, connected),
        "channel": channel,
        "has_client": bool(config.youtube.client_id and config.youtube.client_secret),
        "has_api_key": bool(config.youtube.api_key),
        "token_present": store.read_token() is not None,
        "redirect_uri": redirect_uri(config, request_base_url),
        "public_url": config.public_url,
        "public_url_managed_by_env": bool(env().public_url),
        "effective_public_url": public_base_url(config, request_base_url),
    }
