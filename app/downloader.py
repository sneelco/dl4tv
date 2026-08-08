"""yt-dlp wrapper: option building, error classification and NFO sidecars."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import DownloadDefaults, ErrorKind, Mapping
from .youtube import VideoInfo

log = logging.getLogger("dl4tv.downloader")

# Error text -> (kind, is it worth retrying?). First match wins, so the
# specific patterns have to come before the generic ones.
_ERROR_PATTERNS: list[tuple[re.Pattern[str], ErrorKind, bool]] = [
    (re.compile(r"drm", re.I), "drm", True),
    (re.compile(r"private video|video is private", re.I), "private", True),
    (
        re.compile(r"members[- ]only|join this channel|available to this channel's members", re.I),
        "members_only",
        True,
    ),
    (
        re.compile(r"confirm your age|age[- ]restricted|inappropriate for some users", re.I),
        "age_restricted",
        True,
    ),
    (
        re.compile(
            r"available in your country|blocked it in your country|"
            r"blocked in your country|geo[- ]?restrict",
            re.I,
        ),
        "geo_blocked",
        True,
    ),
    (
        re.compile(
            r"video unavailable|has been removed|no longer available|"
            r"account associated with this video has been terminated|removed by the uploader",
            re.I,
        ),
        "unavailable",
        True,
    ),
    # An install problem rather than a video problem -- see sync.py, which
    # deliberately does not count these against a video's retry budget.
    (
        re.compile(r"ffmpeg is not installed|ffprobe.*not installed|ffmpeg not found", re.I),
        "no_ffmpeg",
        False,
    ),
    (re.compile(r"not a bot|sign in to confirm", re.I), "bot_check", False),
    (
        re.compile(r"live event will begin|premieres in|is live|live stream recording", re.I),
        "live_or_upcoming",
        False,
    ),
    (re.compile(r"no space left on device|disk quota|read-only file system", re.I), "disk", False),
    (
        re.compile(
            r"timed out|timeout|connection|temporary failure in name resolution|"
            r"network|http error 5\d\d|http error 403|unable to connect|remote end closed|"
            r"proxy",
            re.I,
        ),
        "network",
        False,
    ),
]


def classify_error(message: str) -> tuple[ErrorKind, bool]:
    """Map a yt-dlp error message to ``(kind, permanent)``.

    Permanent means "retrying will not help" -- DRM, deleted videos,
    members-only content and so on. Those stop consuming attempts and are
    surfaced in the UI instead, where they can be cleared by hand.
    """
    text = message or ""
    for pattern, kind, permanent in _ERROR_PATTERNS:
        if pattern.search(text):
            return kind, permanent
    return "unknown", False


@dataclass
class DownloadOutcome:
    ok: bool
    path: str | None = None
    filesize: int | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    permanent: bool = False


class _YdlLogger:
    """Route yt-dlp chatter into our logger and remember the last error."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            return
        log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        log.debug("yt-dlp error: %s", msg)


def build_ydl_opts(
    defaults: DownloadDefaults,
    mapping: Mapping | None,
    destination: Path,
    logger: Any | None = None,
    progress_hook: Callable[[dict], None] | None = None,
) -> dict:
    template = (mapping.output_template if mapping else None) or defaults.output_template
    opts: dict[str, Any] = {
        "format": (mapping.format if mapping else None) or defaults.format,
        "merge_output_format": defaults.merge_output_format,
        "outtmpl": {"default": str(destination / template)},
        "paths": {"home": str(destination)},
        "noprogress": True,
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": False,
        "noplaylist": True,
        "retries": defaults.retries,
        "fragment_retries": defaults.retries,
        "concurrent_fragment_downloads": defaults.concurrent_fragments,
        "continuedl": True,
        # Never leave a half-finished file where ErsatzTV might scan it.
        "windowsfilenames": False,
        "restrictfilenames": False,
        "overwrites": False,
        "postprocessors": [],
    }
    if logger is not None:
        opts["logger"] = logger
    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]
    if defaults.cookies_file:
        opts["cookiefile"] = defaults.cookies_file
    if defaults.rate_limit:
        opts["ratelimit"] = _parse_rate(defaults.rate_limit)
    if defaults.write_thumbnail:
        opts["writethumbnail"] = True
    if defaults.write_subtitles or defaults.embed_subtitles:
        opts["writesubtitles"] = True
        opts["subtitleslangs"] = [
            lang.strip() for lang in defaults.subtitle_languages.split(",") if lang.strip()
        ]

    postprocessors: list[dict[str, Any]] = opts["postprocessors"]
    if defaults.sponsorblock_remove:
        postprocessors.append(
            {
                "key": "SponsorBlock",
                "categories": list(defaults.sponsorblock_remove),
                "when": "after_filter",
            }
        )
        postprocessors.append(
            {
                "key": "ModifyChapters",
                "remove_sponsor_segments": list(defaults.sponsorblock_remove),
            }
        )
    if defaults.embed_subtitles:
        postprocessors.append({"key": "FFmpegEmbedSubtitle"})
    if defaults.embed_metadata:
        postprocessors.append(
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True}
        )
    if defaults.embed_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail"})
    return opts


def _parse_rate(value: str) -> int | None:
    """``"5M"`` -> bytes per second."""
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([KMG]?)i?B?/?s?\s*$", value, re.I)
    if not match:
        return None
    scale = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2).upper()]
    return int(float(match.group(1)) * scale)


def _result_path(info: dict) -> str | None:
    downloads = info.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return downloads[0]["filepath"]
    return info.get("filepath") or info.get("_filename")


def download_video(
    video_id: str,
    destination: Path,
    defaults: DownloadDefaults,
    mapping: Mapping | None = None,
    progress_hook: Callable[[dict], None] | None = None,
    video: VideoInfo | None = None,
) -> DownloadOutcome:
    """Download one video. Blocking -- call it from a worker thread."""
    import yt_dlp  # noqa: PLC0415 - keeps import cost off the request path

    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DownloadOutcome(
            ok=False,
            error=f"Cannot create download folder {destination}: {exc}",
            error_kind="disk",
            permanent=False,
        )

    logger = _YdlLogger()
    opts = build_ydl_opts(defaults, mapping, destination, logger, progress_hook)
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises a wide variety
        message = str(exc).strip() or exc.__class__.__name__
        if logger.errors:
            message = logger.errors[-1].strip() or message
        kind, permanent = classify_error(message)
        log.warning("download failed for %s: %s (%s)", video_id, message, kind)
        return DownloadOutcome(
            ok=False, error=_clean(message), error_kind=kind, permanent=permanent
        )

    if not info:
        return DownloadOutcome(
            ok=False,
            error="yt-dlp returned no information for this video",
            error_kind="unknown",
        )

    path = _result_path(info)
    size = None
    if path and Path(path).exists():
        size = Path(path).stat().st_size
    elif path:
        log.debug("yt-dlp reported %s but the file is missing", path)

    if mapping is not None and mapping.write_nfo and path:
        try:
            write_nfo(Path(path), info, mapping.nfo_kind, video)
        except OSError as exc:
            log.warning("could not write NFO next to %s: %s", path, exc)

    return DownloadOutcome(ok=True, path=path, filesize=size)


def _clean(message: str) -> str:
    """Trim yt-dlp's ERROR prefix and ANSI noise for display."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", message).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    return text[:1000]


def write_nfo(
    media_path: Path, info: dict, kind: str = "movie", video: VideoInfo | None = None
) -> Path:
    """Write a Kodi-style NFO sidecar next to the downloaded file."""
    root = ET.Element(kind)
    title = info.get("title") or (video.title if video else media_path.stem)
    ET.SubElement(root, "title").text = title
    plot = info.get("description") or (video.description if video else "")
    ET.SubElement(root, "plot").text = plot or ""

    upload_date = info.get("upload_date")
    aired = None
    if upload_date and len(upload_date) == 8:
        aired = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    elif video and video.published_at:
        aired = video.published_at.date().isoformat()
    if aired:
        ET.SubElement(root, "premiered").text = aired
        ET.SubElement(root, "aired").text = aired
        ET.SubElement(root, "year").text = aired[:4]

    channel = info.get("uploader") or info.get("channel") or (
        video.channel_title if video else None
    )
    if channel:
        ET.SubElement(root, "studio").text = channel
        ET.SubElement(root, "director").text = channel

    duration = info.get("duration") or (video.duration if video else None)
    if duration:
        ET.SubElement(root, "runtime").text = str(int(duration) // 60)

    for tag in (info.get("tags") or (video.tags if video else []) or [])[:15]:
        ET.SubElement(root, "tag").text = tag

    video_id = info.get("id") or (video.id if video else None)
    if video_id:
        unique = ET.SubElement(root, "uniqueid", {"type": "youtube", "default": "true"})
        unique.text = video_id
        ET.SubElement(root, "trailer").text = f"https://www.youtube.com/watch?v={video_id}"

    nfo_path = media_path.with_suffix(".nfo")
    ET.ElementTree(root).write(nfo_path, encoding="utf-8", xml_declaration=True)
    return nfo_path
