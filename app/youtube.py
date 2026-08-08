"""YouTube Data API v3 access and the Google OAuth dance.

Two credential modes are supported:

* **OAuth** -- required to list *your* playlists (including private ones) and
  to read private playlist contents.
* **API key** -- enough for public playlists referenced by id or URL.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger("dl4tv.youtube")

API_BASE = "https://www.googleapis.com/youtube/v3"
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
# Playlist id prefixes YouTube hands out (uploads, likes, favourites, ...).
_PLAYLIST_ID_RE = re.compile(r"^(?:PL|UU|LL|FL|OL|RD|SP)[A-Za-z0-9_-]{2,}$|^LL$|^WL$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{20,}$")


class YouTubeError(Exception):
    """Any failure talking to the YouTube API."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class NotAuthenticated(YouTubeError):
    pass


def parse_duration(value: str | None) -> int | None:
    """Turn an ISO-8601 duration (``PT4M13S``) into seconds."""
    if not value:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class PlaylistInfo:
    id: str
    title: str
    description: str = ""
    item_count: int | None = None
    privacy: str | None = None
    thumbnail: str | None = None
    channel_title: str | None = None
    kind: str = "playlist"  # or "uploads" / "likes"


@dataclass
class VideoInfo:
    id: str
    title: str
    description: str = ""
    published_at: datetime | None = None
    channel_title: str | None = None
    duration: int | None = None
    live_state: str | None = None  # "live", "upcoming" or None
    privacy: str | None = None
    thumbnail: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


class YouTubeClient:
    """Thin synchronous wrapper over the REST API.

    Call it from a worker thread -- it uses blocking httpx on purpose so the
    same code path works from tests and from the async request handlers.
    """

    def __init__(
        self,
        access_token: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not access_token and not api_key:
            raise NotAuthenticated(
                "No YouTube credentials configured. Connect a Google account or "
                "set an API key in Settings."
            )
        self.access_token = access_token
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> YouTubeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- plumbing ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict:
        query = {k: v for k, v in params.items() if v is not None}
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        else:
            query["key"] = self.api_key
        try:
            response = self._client.get(
                f"{API_BASE}/{path}", params=query, headers=headers
            )
        except httpx.HTTPError as exc:
            raise YouTubeError(f"Could not reach the YouTube API: {exc}") from exc
        if response.status_code >= 400:
            raise YouTubeError(self._error_message(response), response.status_code)
        return response.json()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        detail = ""
        try:
            error = response.json().get("error", {})
            detail = error.get("message", "")
            reasons = {e.get("reason") for e in error.get("errors", []) if e.get("reason")}
            if "quotaExceeded" in reasons:
                detail = "YouTube API quota exceeded for today. " + detail
            elif "playlistItemsNotAccessible" in reasons or response.status_code == 403:
                detail = detail or "Access denied. The playlist may be private."
        except ValueError:
            detail = response.text[:300]
        return f"YouTube API error {response.status_code}: {detail or 'unknown error'}"

    def _paginate(self, path: str, params: dict[str, Any], cap: int | None = None) -> Iterator[dict]:
        page_token: str | None = None
        seen = 0
        while True:
            payload = self._get(path, {**params, "pageToken": page_token})
            for item in payload.get("items", []):
                yield item
                seen += 1
                if cap is not None and seen >= cap:
                    return
            page_token = payload.get("nextPageToken")
            if not page_token:
                return

    # -- playlists --------------------------------------------------------

    def my_playlists(self) -> list[PlaylistInfo]:
        if not self.access_token:
            raise NotAuthenticated(
                "Listing your own playlists requires a connected Google account."
            )
        playlists = [
            self._playlist_from_item(item)
            for item in self._paginate(
                "playlists",
                {"part": "snippet,contentDetails,status", "mine": "true", "maxResults": 50},
            )
        ]
        playlists.extend(self._related_playlists())
        return playlists

    def _related_playlists(self) -> list[PlaylistInfo]:
        """Uploads / likes pseudo-playlists for the signed-in channel."""
        try:
            payload = self._get(
                "channels", {"part": "contentDetails,snippet", "mine": "true"}
            )
        except YouTubeError as exc:
            log.debug("could not read channel playlists: %s", exc)
            return []
        out: list[PlaylistInfo] = []
        for item in payload.get("items", []):
            related = item.get("contentDetails", {}).get("relatedPlaylists", {})
            channel = item.get("snippet", {}).get("title", "your channel")
            for key, label in (("uploads", "Uploads"), ("likes", "Liked videos")):
                playlist_id = related.get(key)
                if playlist_id:
                    out.append(
                        PlaylistInfo(
                            id=playlist_id,
                            title=f"{label} ({channel})",
                            channel_title=channel,
                            kind=key,
                        )
                    )
        return out

    @staticmethod
    def _playlist_from_item(item: dict) -> PlaylistInfo:
        snippet = item.get("snippet", {})
        thumbs = snippet.get("thumbnails", {})
        thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
        return PlaylistInfo(
            id=item.get("id", ""),
            title=snippet.get("title", "(untitled)"),
            description=snippet.get("description", ""),
            item_count=item.get("contentDetails", {}).get("itemCount"),
            privacy=item.get("status", {}).get("privacyStatus"),
            thumbnail=thumb,
            channel_title=snippet.get("channelTitle"),
        )

    def get_playlist(self, playlist_id: str) -> PlaylistInfo | None:
        payload = self._get(
            "playlists",
            {"part": "snippet,contentDetails,status", "id": playlist_id, "maxResults": 1},
        )
        items = payload.get("items", [])
        return self._playlist_from_item(items[0]) if items else None

    def resolve_playlist(self, text: str) -> PlaylistInfo:
        """Accept a playlist id/URL, or a channel URL/id (uses its uploads)."""
        playlist_id = self._extract_playlist_id(text)
        if playlist_id is None:
            playlist_id = self._channel_uploads_id(text)
        if playlist_id is None:
            raise YouTubeError(
                "Could not find a playlist in that value. Paste a playlist URL, a "
                "playlist id, or a channel URL."
            )
        info = self.get_playlist(playlist_id)
        if info is None:
            # Uploads playlists resolve fine even when playlists.list hides them.
            info = PlaylistInfo(id=playlist_id, title=playlist_id)
        return info

    @staticmethod
    def _extract_playlist_id(text: str) -> str | None:
        value = (text or "").strip()
        if not value:
            return None
        match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", value)
        if match:
            return match.group(1)
        if "/" not in value and _PLAYLIST_ID_RE.match(value):
            return value
        return None

    def _channel_uploads_id(self, text: str) -> str | None:
        value = (text or "").strip()
        channel_id = None
        handle = None
        username = None

        if _CHANNEL_ID_RE.match(value):
            channel_id = value
        elif match := re.search(r"/channel/(UC[A-Za-z0-9_-]+)", value):
            channel_id = match.group(1)
        elif match := re.search(r"/@([A-Za-z0-9_.-]+)", value):
            handle = match.group(1)
        elif value.startswith("@"):
            handle = value[1:]
        elif match := re.search(r"/user/([A-Za-z0-9_.-]+)", value):
            username = match.group(1)

        if channel_id:
            # Uploads playlists are the channel id with a UU prefix.
            return "UU" + channel_id[2:]
        if handle or username:
            params: dict[str, Any] = {"part": "contentDetails"}
            if handle:
                params["forHandle"] = f"@{handle}"
            else:
                params["forUsername"] = username
            payload = self._get("channels", params)
            for item in payload.get("items", []):
                uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get(
                    "uploads"
                )
                if uploads:
                    return uploads
        return None

    # -- videos -----------------------------------------------------------

    def playlist_video_ids(self, playlist_id: str, cap: int | None = None) -> list[str]:
        ids: list[str] = []
        for item in self._paginate(
            "playlistItems",
            {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50},
            cap=cap,
        ):
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id and video_id not in ids:
                ids.append(video_id)
        return ids

    def video_details(self, video_ids: Iterable[str]) -> dict[str, VideoInfo]:
        out: dict[str, VideoInfo] = {}
        ids = list(video_ids)
        for start in range(0, len(ids), 50):
            batch = ids[start : start + 50]
            payload = self._get(
                "videos",
                {"part": "snippet,contentDetails,status", "id": ",".join(batch), "maxResults": 50},
            )
            for item in payload.get("items", []):
                snippet = item.get("snippet", {})
                thumbs = snippet.get("thumbnails", {})
                thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
                live = snippet.get("liveBroadcastContent")
                out[item["id"]] = VideoInfo(
                    id=item["id"],
                    title=snippet.get("title", ""),
                    description=snippet.get("description", ""),
                    published_at=_parse_ts(snippet.get("publishedAt")),
                    channel_title=snippet.get("channelTitle"),
                    duration=parse_duration(item.get("contentDetails", {}).get("duration")),
                    live_state=live if live in {"live", "upcoming"} else None,
                    privacy=item.get("status", {}).get("privacyStatus"),
                    thumbnail=thumb,
                    tags=snippet.get("tags", []) or [],
                )
        return out
