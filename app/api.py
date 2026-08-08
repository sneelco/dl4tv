"""HTTP API consumed by the web UI."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth as gauth
from . import logbuf, security
from .models import AppConfig, DownloadDefaults, Mapping, Schedule, YouTubeConfig
from .settings import env
from .sources import make_source
from .store import get_store
from .sync import get_manager
from .youtube import NotAuthenticated, YouTubeError

log = logging.getLogger("dl4tv.api")
router = APIRouter()

MASK = "•" * 8


def _masked(value: str | None) -> str | None:
    return MASK if value else None


def _unmask(new: str | None, current: str | None) -> str | None:
    """Keep the stored secret when the UI sends the mask back unchanged."""
    if new is None or new == MASK:
        return current
    return new.strip() or None


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------


class MappingCreate(BaseModel):
    # Either a playlist id, or anything resolvable (playlist/channel URL).
    playlist_id: str | None = None
    query: str | None = None
    title: str | None = None
    folder: str
    enabled: bool = True
    format: str | None = None
    output_template: str | None = None
    max_new_per_run: int | None = None
    min_duration_seconds: int | None = None
    max_duration_seconds: int | None = None
    write_nfo: bool = False
    nfo_kind: str = "movie"


class MappingUpdate(BaseModel):
    title: str | None = None
    folder: str | None = None
    enabled: bool | None = None
    format: str | None = None
    output_template: str | None = None
    max_new_per_run: int | None = None
    min_duration_seconds: int | None = None
    max_duration_seconds: int | None = None
    write_nfo: bool | None = None
    nfo_kind: str | None = None


class SettingsUpdate(BaseModel):
    # The address browsers use to reach dl4tv; "" clears it.
    public_url: str | None = None
    schedule: Schedule | None = None
    downloads: DownloadDefaults | None = None
    youtube: YouTubeConfig | None = None


class ResolveRequest(BaseModel):
    query: str


class SyncRequest(BaseModel):
    mapping_ids: list[str] | None = None


class FolderRequest(BaseModel):
    path: str = Field(min_length=1)


class UnlockRequest(BaseModel):
    passphrase: str


class PassphraseRequest(BaseModel):
    # Empty (or null) removes the lock and reopens the UI.
    passphrase: str | None = None


def _clean_public_url(value: str) -> str | None:
    """Normalise the browser-facing base URL, or reject something unusable."""
    url = (value or "").strip().rstrip("/")
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="Enter a full URL including the scheme, e.g. https://dl4tv.example.com",
        )
    return url


# --------------------------------------------------------------------------
# status, settings
# --------------------------------------------------------------------------


@router.get("/api/status")
async def status(request: Request) -> dict:
    store = get_store()
    manager = get_manager(store)
    state = store.state
    config = store.config
    playlists = []
    for mapping in config.mappings:
        playlist = state.playlists.get(mapping.id)
        playlists.append(
            {
                "id": mapping.id,
                "title": mapping.title,
                "playlist_id": mapping.playlist_id,
                "folder": mapping.folder,
                "resolved_folder": str(store.resolve_folder(mapping.folder)),
                "enabled": mapping.enabled,
                "last_sync_at": playlist.last_sync_at if playlist else None,
                "last_status": playlist.last_status if playlist else "never",
                "last_error": playlist.last_error if playlist else None,
                "counts": playlist.counts() if playlist else
                {"downloaded": 0, "failed": 0, "skipped": 0, "permanent": 0},
            }
        )
    return {
        "running": manager.running,
        "cancelling": manager.cancelling,
        "progress": manager.progress,
        "next_run_at": manager.next_run_at,
        "schedule": config.schedule,
        "download_dir": str(env().download_dir),
        "config_dir": str(env().config_dir),
        "auth": gauth.auth_status(store, str(request.base_url)),
        "playlists": playlists,
        "runs": state.runs[:10],
    }


@router.get("/api/settings")
async def get_settings() -> dict:
    config = get_store().config
    return {
        "public_url": config.public_url,
        "public_url_managed_by_env": bool(env().public_url),
        "schedule": config.schedule,
        "downloads": config.downloads,
        "youtube": {
            "source": config.youtube.source,
            "api_key": _masked(config.youtube.api_key),
            "client_id": config.youtube.client_id,
            "client_secret": _masked(config.youtube.client_secret),
        },
    }


@router.put("/api/settings")
async def put_settings(payload: SettingsUpdate) -> dict:
    store = get_store()
    # Validate before touching the stored config, so a bad URL cannot leave a
    # half-applied settings save behind.
    public_url = (
        _clean_public_url(payload.public_url) if payload.public_url is not None else None
    )

    def mutate(config: AppConfig) -> None:
        if payload.public_url is not None:
            config.public_url = public_url
        if payload.schedule is not None:
            config.schedule = payload.schedule
        if payload.downloads is not None:
            config.downloads = payload.downloads
        if payload.youtube is not None:
            config.youtube = YouTubeConfig(
                source=payload.youtube.source,
                api_key=_unmask(payload.youtube.api_key, config.youtube.api_key),
                client_id=(payload.youtube.client_id or "").strip() or None,
                client_secret=_unmask(
                    payload.youtube.client_secret, config.youtube.client_secret
                ),
            )

    store.update_config(mutate)
    get_manager(store).reschedule()
    log.info("settings updated")
    return await get_settings()


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


@router.get("/api/auth/status")
async def auth_status(request: Request) -> dict:
    return gauth.auth_status(get_store(), str(request.base_url))


@router.get("/auth/start")
async def auth_start(request: Request) -> Any:
    store = get_store()
    uri = gauth.redirect_uri(store.config, str(request.base_url))
    try:
        url, _state = gauth.authorization_url(store.config, uri)
    except NotAuthenticated as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not start OAuth: {exc}") from exc
    return RedirectResponse(url, status_code=307)


@router.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if error:
        return _callback_page(f"Google returned an error: {error}", ok=False)
    if not code:
        return _callback_page("No authorization code was returned.", ok=False)
    store = get_store()
    try:
        await asyncio.to_thread(gauth.exchange_code, store, store.config, code, state)
    except Exception as exc:  # noqa: BLE001
        log.error("OAuth exchange failed: %s", exc)
        return _callback_page(f"Could not complete sign-in: {exc}", ok=False)
    return _callback_page("YouTube account connected. You can close this tab.", ok=True)


def _callback_page(message: str, ok: bool) -> HTMLResponse:
    colour = "#3fb950" if ok else "#f85149"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<title>dl4tv</title>
<body style="font-family:system-ui;background:#0d1117;color:#e6edf3;padding:3rem;text-align:center">
<h2 style="color:{colour}">{'Connected' if ok else 'Sign-in failed'}</h2>
<p>{message}</p>
<p><a style="color:#58a6ff" href="/">Back to dl4tv</a></p>
</body>""",
        status_code=200 if ok else 400,
    )


@router.post("/api/auth/disconnect")
async def auth_disconnect() -> dict:
    get_store().clear_token()
    log.info("disconnected YouTube account")
    return {"ok": True}


# --------------------------------------------------------------------------
# access lock
# --------------------------------------------------------------------------

# A locked instance is usually on a LAN, so the guard here is deliberately
# light: enough to make guessing a passphrase impractical, not a full
# rate-limiter.
_MAX_FAILURES = 10
_FAILURE_WINDOW_SECONDS = 300
_failures: dict[str, list[float]] = {}


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttled(request: Request) -> bool:
    now = time.monotonic()
    key = _client_key(request)
    recent = [t for t in _failures.get(key, []) if now - t < _FAILURE_WINDOW_SECONDS]
    _failures[key] = recent
    return len(recent) >= _MAX_FAILURES


def _record_failure(request: Request) -> None:
    _failures.setdefault(_client_key(request), []).append(time.monotonic())


def _issue_session(response: Response, request: Request, store) -> None:
    token = security.issue_token(
        store.session_secret(), security.credential_fingerprint(store.config, env())
    )
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        max_age=security.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.get("/api/access")
async def access_status(request: Request) -> dict:
    """Open endpoint: the unlock page needs it before anyone is authenticated."""
    from .main import authenticated  # noqa: PLC0415 - avoids an import cycle

    store = get_store()
    settings = env()
    locked = security.is_locked(store.config, settings)
    return {
        "locked": locked,
        "authenticated": not locked or authenticated(request, store, settings),
        "managed_by_env": security.managed_by_env(settings),
    }


@router.post("/api/access/unlock")
async def unlock(payload: UnlockRequest, request: Request, response: Response) -> dict:
    store = get_store()
    settings = env()
    if not security.is_locked(store.config, settings):
        return {"ok": True, "locked": False}
    if _throttled(request):
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Wait a few minutes and try again.",
        )
    if not security.check_passphrase(store.config, settings, payload.passphrase):
        _record_failure(request)
        log.warning("failed unlock attempt from %s", _client_key(request))
        raise HTTPException(status_code=401, detail="That passphrase is not correct.")
    _failures.pop(_client_key(request), None)
    _issue_session(response, request, store)
    log.info("unlocked from %s", _client_key(request))
    return {"ok": True, "locked": True}


@router.post("/api/access/lock")
async def lock(response: Response) -> dict:
    """Drop this browser's session. The passphrase itself is unchanged."""
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.put("/api/access/passphrase")
async def set_passphrase(
    payload: PassphraseRequest, request: Request, response: Response
) -> dict:
    store = get_store()
    settings = env()
    if security.managed_by_env(settings):
        raise HTTPException(
            status_code=409,
            detail="The passphrase is set by DL4TV_PASSPHRASE and cannot be changed here.",
        )
    passphrase = (payload.passphrase or "").strip()

    if not passphrase:
        store.update_config(lambda config: setattr(config.security, "passphrase_hash", None))
        response.delete_cookie(security.SESSION_COOKIE, path="/")
        log.info("passphrase removed -- the UI is open again")
        return {"locked": False}

    if len(passphrase) < 8:
        raise HTTPException(
            status_code=400, detail="Use at least 8 characters."
        )
    hashed = await asyncio.to_thread(security.hash_passphrase, passphrase)
    store.update_config(lambda config: setattr(config.security, "passphrase_hash", hashed))
    # Hand this browser a fresh session so setting a passphrase does not lock
    # out the person who just set it.
    _issue_session(response, request, store)
    log.info("passphrase set -- the UI is now locked")
    return {"locked": True}

# --------------------------------------------------------------------------
# youtube lookups
# --------------------------------------------------------------------------


@router.get("/api/youtube/playlists")
async def list_playlists() -> dict:
    store = get_store()

    def work() -> list[dict]:
        with make_source(store) as client:
            return [vars(p) for p in client.my_playlists()]

    try:
        playlists = await asyncio.to_thread(work)
    except NotAuthenticated as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except YouTubeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"playlists": playlists}


@router.post("/api/youtube/resolve")
async def resolve_playlist(payload: ResolveRequest) -> dict:
    store = get_store()

    def work() -> dict:
        with make_source(store) as client:
            return vars(client.resolve_playlist(payload.query))

    try:
        return await asyncio.to_thread(work)
    except NotAuthenticated as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except YouTubeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# mappings
# --------------------------------------------------------------------------


@router.get("/api/mappings")
async def list_mappings() -> dict:
    store = get_store()
    return {
        "mappings": store.config.mappings,
        "download_dir": str(env().download_dir),
    }


@router.post("/api/mappings", status_code=201)
async def create_mapping(payload: MappingCreate) -> Mapping:
    store = get_store()
    playlist_id = (payload.playlist_id or "").strip()
    title = (payload.title or "").strip()

    if not playlist_id:
        if not payload.query:
            raise HTTPException(
                status_code=400, detail="Provide a playlist id or a playlist URL."
            )

        def work() -> Any:
            with make_source(store) as client:
                return client.resolve_playlist(payload.query or "")

        try:
            info = await asyncio.to_thread(work)
        except NotAuthenticated as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except YouTubeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        playlist_id = info.id
        title = title or info.title

    if any(m.playlist_id == playlist_id for m in store.config.mappings):
        raise HTTPException(status_code=409, detail="That playlist is already mapped.")

    mapping = Mapping(
        playlist_id=playlist_id,
        title=title or playlist_id,
        folder=payload.folder.strip(),
        enabled=payload.enabled,
        format=payload.format or None,
        output_template=payload.output_template or None,
        max_new_per_run=payload.max_new_per_run,
        min_duration_seconds=payload.min_duration_seconds,
        max_duration_seconds=payload.max_duration_seconds,
        write_nfo=payload.write_nfo,
        nfo_kind=payload.nfo_kind,  # type: ignore[arg-type]
    )
    store.update_config(lambda config: config.mappings.append(mapping))
    log.info("mapped playlist %r -> %s", mapping.title, mapping.folder)
    return mapping


@router.patch("/api/mappings/{mapping_id}")
async def update_mapping(mapping_id: str, payload: MappingUpdate) -> Mapping:
    store = get_store()
    if store.config.mapping(mapping_id) is None:
        raise HTTPException(status_code=404, detail="No such mapping.")

    def mutate(config: AppConfig) -> None:
        mapping = config.mapping(mapping_id)
        assert mapping is not None
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(mapping, field, value)

    config = store.update_config(mutate)
    updated = config.mapping(mapping_id)
    assert updated is not None
    return updated


@router.delete("/api/mappings/{mapping_id}")
async def delete_mapping(
    mapping_id: str, forget: bool = Query(default=True, description="Drop download history too")
) -> dict:
    store = get_store()
    if store.config.mapping(mapping_id) is None:
        raise HTTPException(status_code=404, detail="No such mapping.")
    def mutate(config: AppConfig) -> None:
        config.mappings = [m for m in config.mappings if m.id != mapping_id]

    store.update_config(mutate)
    if forget:
        store.update_state(lambda state: state.playlists.pop(mapping_id, None))
    log.info("removed mapping %s", mapping_id)
    return {"ok": True}


@router.get("/api/mappings/{mapping_id}/videos")
async def mapping_videos(mapping_id: str) -> dict:
    store = get_store()
    mapping = store.config.mapping(mapping_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="No such mapping.")
    playlist = store.state.playlists.get(mapping_id)
    videos = sorted(
        (playlist.videos.values() if playlist else []),
        key=lambda v: (v.downloaded_at or v.last_attempt_at or v.published_at or ""),
        reverse=True,
    )
    return {
        "mapping": mapping,
        "last_sync_at": playlist.last_sync_at if playlist else None,
        "last_status": playlist.last_status if playlist else "never",
        "last_error": playlist.last_error if playlist else None,
        "videos": videos,
    }


@router.post("/api/mappings/{mapping_id}/videos/{video_id}/retry")
async def retry_video(mapping_id: str, video_id: str) -> dict:
    store = get_store()

    def mutate(state) -> None:
        playlist = state.playlists.get(mapping_id)
        record = playlist.videos.get(video_id) if playlist else None
        if record is not None:
            record.permanent = False
            record.attempts = 0
            record.error = None
            record.error_kind = None
            record.reason = None
            record.status = "failed"

    store.update_state(mutate)
    return {"ok": True}


@router.delete("/api/mappings/{mapping_id}/videos/{video_id}")
async def forget_video(mapping_id: str, video_id: str) -> dict:
    def mutate(state) -> None:
        playlist = state.playlists.get(mapping_id)
        if playlist:
            playlist.videos.pop(video_id, None)

    get_store().update_state(mutate)
    return {"ok": True}


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


@router.post("/api/sync")
async def start_sync(payload: SyncRequest | None = None) -> dict:
    manager = get_manager(get_store())
    started = manager.trigger(mapping_ids=payload.mapping_ids if payload else None)
    if not started:
        raise HTTPException(status_code=409, detail="A sync is already running.")
    return {"started": True}


@router.post("/api/mappings/{mapping_id}/sync")
async def sync_one(mapping_id: str) -> dict:
    store = get_store()
    if store.config.mapping(mapping_id) is None:
        raise HTTPException(status_code=404, detail="No such mapping.")
    if not get_manager(store).trigger(mapping_ids=[mapping_id]):
        raise HTTPException(status_code=409, detail="A sync is already running.")
    return {"started": True}


@router.post("/api/sync/cancel")
async def cancel_sync() -> dict:
    return {"cancelled": get_manager(get_store()).cancel()}


@router.get("/api/runs")
async def list_runs() -> dict:
    return {"runs": get_store().state.runs}


@router.get("/api/logs")
async def get_logs(since: int = 0, limit: int = 200) -> dict:
    return {"logs": logbuf.handler.records(since=since, limit=limit)}


# --------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------


def _safe_join(relative: str) -> Path:
    root = env().download_dir.resolve()
    candidate = (root / relative.strip("/")).resolve() if relative else root
    if candidate != root and root not in candidate.parents:
        raise HTTPException(
            status_code=400, detail="Path is outside the download directory."
        )
    return candidate


@router.get("/api/folders")
async def list_folders(path: str = "") -> dict:
    root = env().download_dir
    target = _safe_join(path)
    entries: list[dict] = []
    if target.is_dir():
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if child.is_dir() and not child.name.startswith("."):
                    entries.append(
                        {
                            "name": child.name,
                            "path": str(child.relative_to(root.resolve())),
                        }
                    )
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "root": str(root),
        "path": path,
        "exists": target.is_dir(),
        "folders": entries,
    }


@router.post("/api/folders", status_code=201)
async def create_folder(payload: FolderRequest) -> dict:
    target = _safe_join(payload.path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not create {target}: {exc}") from exc
    return {"ok": True, "path": payload.path, "resolved": str(target)}
