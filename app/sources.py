"""Chooses where playlist listings come from.

Two interchangeable sources:

``api``     the YouTube Data API -- needs an OAuth account or an API key, and
            is the only way to see your own private playlists.
``yt-dlp``  scrapes the playlist page -- no Google account, no API key, no
            quota, but public playlists only.

``auto`` (the default) uses the API when credentials exist and falls back to
yt-dlp when they do not, so a fresh install works out of the box.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .models import AppConfig
from .store import Store
from .youtube import PlaylistInfo, VideoInfo, YouTubeClient
from .ytdlp import YtDlpSource

log = logging.getLogger("dl4tv.sources")


class PlaylistSource(Protocol):
    """What the sync engine needs from a source."""

    def my_playlists(self) -> list[PlaylistInfo]: ...

    def resolve_playlist(self, text: str) -> PlaylistInfo: ...

    def playlist_video_ids(self, playlist_id: str, cap: int | None = None) -> list[str]: ...

    def video_details(self, video_ids: list[str]) -> dict[str, VideoInfo]: ...

    def close(self) -> None: ...


def effective_source(config: AppConfig, has_token: bool) -> str:
    """Resolve ``auto`` against the credentials actually available."""
    configured = config.youtube.source
    if configured != "auto":
        return configured
    return "api" if (has_token or config.youtube.api_key) else "yt-dlp"


def make_source(store: Store) -> PlaylistSource:
    from .auth import access_token  # noqa: PLC0415 - avoids an import cycle

    config = store.config
    token = access_token(store)
    name = effective_source(config, bool(token))
    if name == "api":
        return YouTubeClient(access_token=token, api_key=config.youtube.api_key)
    log.debug("using yt-dlp to read playlists (no credentials required)")
    return YtDlpSource(cookies_file=config.downloads.cookies_file)
