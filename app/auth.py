"""Google OAuth handling: build the consent URL, swap the code for a token,
keep the refresh token on disk and hand out fresh access tokens.
"""

from __future__ import annotations

import logging
import os
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


def redirect_uri(request_base_url: str | None = None) -> str:
    """Where Google sends the browser back to after consent."""
    base = env().public_url or (request_base_url or "").rstrip("/")
    return f"{base}/auth/callback"


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


def authorization_url(config: AppConfig, uri: str) -> tuple[str, str]:
    flow = build_flow(config, uri)
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        # Force a refresh token even when the account has consented before.
        prompt="consent",
    )
    return url, state


def exchange_code(store: Store, config: AppConfig, uri: str, code: str) -> None:
    flow = build_flow(config, uri)
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
        "redirect_uri": redirect_uri(request_base_url),
    }
