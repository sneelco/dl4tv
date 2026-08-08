from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.downloader import _parse_rate, build_ydl_opts, classify_error, write_nfo
from app.models import DownloadDefaults, Mapping


@pytest.mark.parametrize(
    ("message", "kind", "permanent"),
    [
        ("ERROR: [youtube] abc: This video is DRM protected", "drm", True),
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "private", True),
        ("Join this channel to get access to members-only content", "members_only", True),
        ("Sign in to confirm your age. This video may be inappropriate for some users.", "age_restricted", True),
        ("The uploader has not made this video available in your country", "geo_blocked", True),
        ("Video unavailable. This video has been removed by the uploader", "unavailable", True),
        ("Sign in to confirm you're not a bot", "bot_check", False),
        ("This live event will begin in 3 hours", "live_or_upcoming", False),
        ("OSError: [Errno 28] No space left on device", "disk", False),
        ("Unable to download webpage: The read operation timed out", "network", False),
        ("something nobody has ever seen before", "unknown", False),
    ],
)
def test_classify_error(message, kind, permanent):
    assert classify_error(message) == (kind, permanent)


def test_classify_error_prefers_specific_match():
    # Mentions both a network verb and DRM; DRM wins because it is permanent.
    kind, permanent = classify_error("Unable to connect: this video is DRM protected")
    assert (kind, permanent) == ("drm", True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5M", 5 * 1024**2), ("500K", 500 * 1024), ("1.5M", int(1.5 * 1024**2)), ("2G", 2 * 1024**3), ("nonsense", None)],
)
def test_parse_rate(value, expected):
    assert _parse_rate(value) == expected


def test_build_ydl_opts_mapping_overrides_defaults(tmp_path):
    defaults = DownloadDefaults(format="best", sponsorblock_remove=["sponsor"])
    mapping = Mapping(
        playlist_id="PL1", title="t", folder="f", format="bestvideo+bestaudio",
        output_template="%(id)s.%(ext)s",
    )
    opts = build_ydl_opts(defaults, mapping, tmp_path)

    assert opts["format"] == "bestvideo+bestaudio"
    assert opts["outtmpl"]["default"] == str(tmp_path / "%(id)s.%(ext)s")
    keys = [pp["key"] for pp in opts["postprocessors"]]
    assert "SponsorBlock" in keys and "ModifyChapters" in keys
    assert "FFmpegMetadata" in keys


def test_build_ydl_opts_without_mapping_uses_defaults(tmp_path):
    defaults = DownloadDefaults(embed_metadata=False, rate_limit="1M", cookies_file="/c.txt")
    opts = build_ydl_opts(defaults, None, tmp_path)

    assert opts["format"] == defaults.format
    assert opts["ratelimit"] == 1024**2
    assert opts["cookiefile"] == "/c.txt"
    assert [pp["key"] for pp in opts["postprocessors"]] == []


def test_write_nfo(tmp_path):
    media = tmp_path / "Some Video [abc123].mkv"
    media.write_bytes(b"")
    info = {
        "id": "abc123",
        "title": "Some Video",
        "description": "A description",
        "upload_date": "20240115",
        "uploader": "A Channel",
        "duration": 605,
        "tags": ["cooking", "how-to"],
    }

    nfo = write_nfo(media, info)

    assert nfo == tmp_path / "Some Video [abc123].nfo"
    root = ET.parse(nfo).getroot()
    assert root.tag == "movie"
    assert root.findtext("title") == "Some Video"
    assert root.findtext("premiered") == "2024-01-15"
    assert root.findtext("studio") == "A Channel"
    assert root.findtext("runtime") == "10"
    assert [t.text for t in root.findall("tag")] == ["cooking", "how-to"]
    assert root.findtext("uniqueid") == "abc123"
