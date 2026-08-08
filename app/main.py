"""Application entry point."""

from __future__ import annotations

import base64
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import logbuf, security
from .api import router
from .settings import Env, env
from .store import Store, get_store
from .sync import get_manager

STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger("dl4tv")


def configure_logging() -> None:
    settings = env()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logbuf.install(settings.log_level)
    # yt-dlp is chatty; we surface its errors ourselves.
    logging.getLogger("yt_dlp").setLevel(logging.ERROR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = env()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    store = get_store()
    log.info(
        "dl4tv starting -- config=%s downloads=%s, %d playlist mapping(s)",
        settings.config_dir,
        settings.download_dir,
        len(store.config.mappings),
    )
    if security.is_locked(store.config, settings):
        how = "DL4TV_PASSPHRASE" if security.managed_by_env(settings) else "Settings"
        log.info("UI locked with a passphrase (set via %s)", how)
    else:
        log.info("UI is open -- set a passphrase in Settings to lock it")
    manager = get_manager(store)
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()
        store.save_state()
        log.info("dl4tv stopped")


def authenticated(request: Request, store: Store, settings: Env) -> bool:
    """A valid session cookie, or HTTP basic with the passphrase as password.

    The basic-auth path exists so scripts and `curl -u :passphrase` keep
    working against a locked instance.
    """
    fingerprint = security.credential_fingerprint(store.config, settings)
    cookie = request.cookies.get(security.SESSION_COOKIE)
    if security.verify_token(cookie, store.session_secret(), fingerprint):
        return True

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _user, _, candidate = decoded.partition(":")
        return security.check_passphrase(store.config, settings, candidate)
    return False


def create_app() -> FastAPI:
    app = FastAPI(
        title="dl4tv",
        description="Download flagged YouTube playlists into folders for ErsatzTV.",
        lifespan=lifespan,
    )
    # Reachable without a passphrase: the health probe, the unlock page itself,
    # and the endpoints that page needs to work.
    open_paths = {"/healthz", "/login", "/api/access", "/api/access/unlock"}

    @app.middleware("http")
    async def access_gate(request, call_next):
        path = request.url.path
        if path in open_paths or path.startswith("/static/"):
            return await call_next(request)

        store = get_store()
        settings = env()
        if not security.is_locked(store.config, settings):
            return await call_next(request)
        if authenticated(request, store, settings):
            return await call_next(request)

        if path.startswith("/api/") or path.startswith("/auth/"):
            return JSONResponse(
                {"detail": "dl4tv is locked. Unlock it with your passphrase."},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="dl4tv"'},
            )
        return RedirectResponse("/login", status_code=307)

    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "login.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()


def main() -> None:
    import uvicorn  # noqa: PLC0415

    settings = env()
    configure_logging()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
