"""Pydantic models for everything dl4tv persists.

Two files hold all state:

``config.yaml``  user intent -- schedule, download options, playlist mappings.
``state.json``   what actually happened -- per-video results, run history.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# config.yaml
# --------------------------------------------------------------------------


class Schedule(BaseModel):
    enabled: bool = True
    mode: Literal["daily", "interval"] = "daily"
    daily_at: str = "03:00"
    interval_minutes: int = Field(default=360, ge=5)
    timezone: str = "UTC"
    run_on_start: bool = False


class DownloadDefaults(BaseModel):
    """Defaults applied to every mapping; individual mappings may override."""

    format: str = "bestvideo*+bestaudio/best"
    merge_output_format: str = "mkv"
    output_template: str = "%(title)s [%(id)s].%(ext)s"
    embed_metadata: bool = True
    # Chapters are written into mp4 as a separate text track, and players that
    # cannot read it may refuse the whole file. Off by default for that reason.
    embed_chapters: bool = False
    # Embedding cover art adds a second video stream. Some TVs then play the
    # still image instead of the video, or reject the file outright.
    embed_thumbnail: bool = False
    write_thumbnail: bool = False
    write_subtitles: bool = False
    embed_subtitles: bool = False
    subtitle_languages: str = "en"
    # SponsorBlock categories to cut out entirely, e.g. ["sponsor", "intro"].
    sponsorblock_remove: list[str] = Field(default_factory=list)
    # Path to a Netscape-format cookies file; needed for age-restricted or
    # members-only videos, and sometimes to get past bot checks.
    cookies_file: str | None = None
    rate_limit: str | None = None
    concurrent_fragments: int = Field(default=4, ge=1, le=16)
    # yt-dlp's own retry count for a single download attempt.
    retries: int = Field(default=3, ge=0)
    # How many times dl4tv re-tries a video across runs before giving up.
    max_attempts: int = Field(default=3, ge=1)
    max_new_per_run: int | None = None


class YouTubeConfig(BaseModel):
    # Where playlist listings come from. "auto" uses the Data API when
    # credentials exist and falls back to yt-dlp when they do not, so public
    # playlists work with no Google setup at all.
    source: Literal["auto", "api", "yt-dlp"] = "auto"
    # Used for public playlists when OAuth is not configured.
    api_key: str | None = None
    # OAuth client, required to list your own (including private) playlists.
    client_id: str | None = None
    client_secret: str | None = None


class SecurityConfig(BaseModel):
    """Optional passphrase lock. Empty means the UI is open to anyone who can
    reach it, which is the default."""

    # scrypt hash, never the passphrase itself.
    passphrase_hash: str | None = None


class Mapping(BaseModel):
    """A YouTube playlist wired to a folder on disk."""

    id: str = Field(default_factory=new_id)
    playlist_id: str
    title: str
    # Absolute, or relative to DL4TV_DOWNLOAD_DIR.
    folder: str
    enabled: bool = True
    format: str | None = None
    output_template: str | None = None
    max_new_per_run: int | None = None
    min_duration_seconds: int | None = None
    max_duration_seconds: int | None = None
    write_nfo: bool = False
    nfo_kind: Literal["movie", "musicvideo", "episodedetails"] = "movie"
    created_at: datetime = Field(default_factory=utcnow)


class AppConfig(BaseModel):
    version: int = 1
    # The address you actually browse to. Only matters for building the OAuth
    # redirect URI, which must match what Google has registered -- behind a
    # reverse proxy or ingress the request's own host is not it.
    public_url: str | None = None
    schedule: Schedule = Field(default_factory=Schedule)
    downloads: DownloadDefaults = Field(default_factory=DownloadDefaults)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    mappings: list[Mapping] = Field(default_factory=list)

    def mapping(self, mapping_id: str) -> Mapping | None:
        return next((m for m in self.mappings if m.id == mapping_id), None)


# --------------------------------------------------------------------------
# state.json
# --------------------------------------------------------------------------

VideoStatus = Literal["downloaded", "failed", "skipped"]

# Error kinds we know we cannot work around by retrying.
ErrorKind = Literal[
    "drm",
    "private",
    "unavailable",
    "members_only",
    "age_restricted",
    "geo_blocked",
    "live_or_upcoming",
    "bot_check",
    "no_ffmpeg",
    "network",
    "disk",
    "unknown",
]


class VideoRecord(BaseModel):
    video_id: str
    title: str = ""
    status: VideoStatus = "failed"
    path: str | None = None
    filesize: int | None = None
    duration: int | None = None
    published_at: datetime | None = None
    downloaded_at: datetime | None = None
    # Why it was skipped (duration filter, live stream, ...).
    reason: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    attempts: int = 0
    last_attempt_at: datetime | None = None
    # Permanent failures are not retried automatically; the UI can clear them.
    permanent: bool = False


class PlaylistState(BaseModel):
    playlist_id: str = ""
    last_sync_at: datetime | None = None
    last_status: Literal["ok", "partial", "error", "never"] = "never"
    last_error: str | None = None
    videos: dict[str, VideoRecord] = Field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        out = {"downloaded": 0, "failed": 0, "skipped": 0, "permanent": 0}
        for record in self.videos.values():
            out[record.status] = out.get(record.status, 0) + 1
            if record.permanent:
                out["permanent"] += 1
        return out


class RunRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    trigger: Literal["schedule", "manual", "startup"] = "manual"
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    mappings: int = 0
    status: Literal["running", "ok", "partial", "error", "cancelled"] = "running"
    error: str | None = None


class AppState(BaseModel):
    version: int = 1
    playlists: dict[str, PlaylistState] = Field(default_factory=dict)
    runs: list[RunRecord] = Field(default_factory=list)

    def playlist(self, mapping_id: str) -> PlaylistState:
        return self.playlists.setdefault(mapping_id, PlaylistState())
