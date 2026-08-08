"""Playlist listing via yt-dlp, so public playlists need no Google credentials.

yt-dlp is already doing the downloading, so asking it to enumerate a playlist
costs nothing extra and works with no API key, no OAuth client, and no quota.
It exposes the same surface as :class:`~app.youtube.YouTubeClient` so the sync
engine cannot tell the two apart.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

from .youtube import (
    _CHANNEL_ID_RE,
    NotAuthenticated,
    PlaylistInfo,
    VideoInfo,
    YouTubeClient,
    YouTubeError,
)

log = logging.getLogger("dl4tv.ytdlp")

# Placeholder titles yt-dlp reports for entries it cannot see into.
_UNAVAILABLE_TITLES = {
    "[private video]",
    "[deleted video]",
    "[unavailable video]",
    "[removed video]",
}
_UNAVAILABLE_AVAILABILITY = {"private", "needs_auth", "premium_only", "subscriber_only"}


def _clean(exc: Exception) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    return re.sub(r"^ERROR:\s*", "", text)[:500]


class YtDlpSource:
    """Read playlists with yt-dlp instead of the YouTube Data API."""

    def __init__(
        self,
        cookies_file: str | None = None,
        extractor: Callable[[str, int | None], dict | None] | None = None,
    ) -> None:
        self.cookies_file = cookies_file
        # Injectable for tests; the default shells out to yt-dlp itself.
        self._extract = extractor or self._extract_with_ytdlp
        # Flat entries from the last playlist read, reused by video_details so
        # a sync costs exactly one playlist fetch.
        self._entries: dict[str, dict] = {}

    def close(self) -> None:
        self._entries.clear()

    def __enter__(self) -> YtDlpSource:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- extraction -------------------------------------------------------

    def _extract_with_ytdlp(self, url: str, limit: int | None = None) -> dict | None:
        import yt_dlp  # noqa: PLC0415

        opts: dict[str, Any] = {
            # Don't resolve every video -- the playlist page already carries
            # the id, title and duration we need.
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            # One broken entry must not lose the whole playlist.
            "ignoreerrors": True,
            "socket_timeout": 30,
        }
        if limit:
            opts["playlistend"] = limit
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _flat(self, url: str, limit: int | None = None) -> dict:
        try:
            info = self._extract(url, limit)
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
            raise YouTubeError(f"yt-dlp could not read {url}: {_clean(exc)}") from exc
        if not info:
            raise YouTubeError(
                f"yt-dlp returned nothing for {url}. The playlist may be private -- "
                "private playlists need a connected Google account."
            )
        return info

    @staticmethod
    def _playlist_url(playlist_id: str) -> str:
        return f"https://www.youtube.com/playlist?list={playlist_id}"

    # -- playlists --------------------------------------------------------

    def my_playlists(self) -> list[PlaylistInfo]:
        raise NotAuthenticated(
            "Listing your own playlists requires a connected Google account. "
            "Without one, add public playlists by URL instead."
        )

    def get_playlist(self, playlist_id: str) -> PlaylistInfo | None:
        info = self._flat(self._playlist_url(playlist_id), limit=1)
        return PlaylistInfo(
            id=playlist_id,
            title=info.get("title") or playlist_id,
            description=info.get("description") or "",
            item_count=info.get("playlist_count"),
            channel_title=info.get("channel") or info.get("uploader"),
        )

    def resolve_playlist(self, text: str) -> PlaylistInfo:
        playlist_id = YouTubeClient._extract_playlist_id(text)
        if playlist_id is None:
            playlist_id = self._channel_uploads_id(text)
        if playlist_id is None:
            raise YouTubeError(
                "Could not find a playlist in that value. Paste a playlist URL, a "
                "playlist id, or a channel URL."
            )
        return self.get_playlist(playlist_id)

    def _channel_uploads_id(self, text: str) -> str | None:
        """Turn a channel reference into its uploads playlist id."""
        value = (text or "").strip()
        if _CHANNEL_ID_RE.match(value):
            return "UU" + value[2:]
        if match := re.search(r"/channel/(UC[A-Za-z0-9_-]+)", value):
            return "UU" + match.group(1)[2:]

        handle_url = None
        if match := re.search(r"/@([A-Za-z0-9_.-]+)", value):
            handle_url = f"https://www.youtube.com/@{match.group(1)}/videos"
        elif value.startswith("@"):
            handle_url = f"https://www.youtube.com/{value}/videos"
        elif match := re.search(r"/user/([A-Za-z0-9_.-]+)", value):
            handle_url = f"https://www.youtube.com/user/{match.group(1)}/videos"
        if handle_url is None:
            return None

        info = self._flat(handle_url, limit=1)
        channel_id = info.get("channel_id") or info.get("uploader_id") or ""
        if channel_id.startswith("UC"):
            return "UU" + channel_id[2:]
        return None

    # -- videos -----------------------------------------------------------

    def playlist_video_ids(self, playlist_id: str, cap: int | None = None) -> list[str]:
        info = self._flat(self._playlist_url(playlist_id), limit=cap)
        ids: list[str] = []
        for entry in info.get("entries") or []:
            if not entry:
                continue
            video_id = entry.get("id")
            if not video_id or video_id in self._entries:
                continue
            self._entries[video_id] = entry
            ids.append(video_id)
        return ids

    def video_details(self, video_ids: Iterable[str]) -> dict[str, VideoInfo]:
        """Details for the given ids.

        Entries yt-dlp could not see into (private, deleted) are left out, the
        same way the Data API omits them, so the sync engine records them as
        unavailable through its existing path.
        """
        out: dict[str, VideoInfo] = {}
        for video_id in video_ids:
            entry = self._entries.get(video_id)
            if entry is None:
                entry = self._fetch_single(video_id)
            if entry is None:
                continue
            info = self._video_from_entry(video_id, entry)
            if info is not None:
                out[video_id] = info
        return out

    def _fetch_single(self, video_id: str) -> dict | None:
        """Fall back to a direct lookup for an id not seen in the playlist."""
        try:
            entry = self._extract(f"https://www.youtube.com/watch?v={video_id}", None)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not read %s: %s", video_id, _clean(exc))
            return None
        if entry:
            self._entries[video_id] = entry
        return entry

    @staticmethod
    def _video_from_entry(video_id: str, entry: dict) -> VideoInfo | None:
        title = (entry.get("title") or "").strip()
        if title.lower() in _UNAVAILABLE_TITLES:
            return None
        if (entry.get("availability") or "") in _UNAVAILABLE_AVAILABILITY:
            return None

        live_status = entry.get("live_status")
        live_state = None
        if live_status == "is_live":
            live_state = "live"
        elif live_status == "is_upcoming":
            live_state = "upcoming"

        duration = entry.get("duration")
        thumbnails = entry.get("thumbnails") or []
        thumbnail = entry.get("thumbnail") or (
            thumbnails[-1].get("url") if thumbnails else None
        )
        return VideoInfo(
            id=video_id,
            title=title or video_id,
            description=entry.get("description") or "",
            channel_title=entry.get("channel") or entry.get("uploader"),
            duration=int(duration) if duration else None,
            live_state=live_state,
            privacy=entry.get("availability"),
            thumbnail=thumbnail,
        )
