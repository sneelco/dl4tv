"""Application entry point."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import logbuf
from .api import router
from .settings import env
from .store import get_store
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
    if settings.auth_enabled:
        log.info("UI protected with HTTP basic auth")
    manager = get_manager(store)
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()
        store.save_state()
        log.info("dl4tv stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="dl4tv",
        description="Download flagged YouTube playlists into folders for ErsatzTV.",
        lifespan=lifespan,
    )
    settings = env()

    if settings.auth_enabled:

        @app.middleware("http")
        async def basic_auth(request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(header[6:]).decode("utf-8")
                    user, _, password = decoded.partition(":")
                except (ValueError, UnicodeDecodeError):
                    user = password = ""
                if secrets.compare_digest(user, settings.username or "") and (
                    secrets.compare_digest(password, settings.password or "")
                ):
                    return await call_next(request)
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="dl4tv"'},
                content="Authentication required",
            )

    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

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
