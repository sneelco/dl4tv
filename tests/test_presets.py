"""Format preset tests.

The point of a preset is that it picks the codecs it claims to. These tests run
yt-dlp's real format selector against a synthetic format list modelled on what
YouTube actually offers, so the selector strings are verified without touching
the network.
"""

from __future__ import annotations

import pytest
import yt_dlp

from app.models import DownloadDefaults
from app.presets import CUSTOM, FORMAT_PRESETS, match_preset, preset


def _format(**kwargs) -> dict:
    kwargs.setdefault("url", "https://example.invalid/media")
    kwargs.setdefault("protocol", "https")
    return kwargs


# yt-dlp treats this list as pre-sorted worst -> best, which is how an
# extractor hands it over. Mirrors a real YouTube video with 4K.
YOUTUBE_FORMATS = [
    _format(format_id="140", ext="m4a", vcodec="none", acodec="mp4a.40.2", abr=128),
    _format(format_id="251", ext="webm", vcodec="none", acodec="opus", abr=130),
    _format(format_id="136", ext="mp4", vcodec="avc1.4d401f", acodec="none", height=720, tbr=2500),
    _format(format_id="299", ext="mp4", vcodec="avc1.64002a", acodec="none", height=1080, tbr=5000),
    _format(format_id="313", ext="webm", vcodec="vp9", acodec="none", height=2160, tbr=11000),
    _format(format_id="401", ext="mp4", vcodec="av01.0.13M.08", acodec="none", height=2160, tbr=12000),
]

# A video with no H.264 at all, to exercise the fallbacks.
VP9_ONLY_FORMATS = [
    _format(format_id="251", ext="webm", vcodec="none", acodec="opus", abr=130),
    _format(format_id="248", ext="webm", vcodec="vp9", acodec="none", height=1080, tbr=4000),
]


def select(format_selector: str, formats: list[dict]) -> list[dict]:
    """Run yt-dlp's own selector and return the chosen format dicts."""
    ydl = yt_dlp.YoutubeDL({"quiet": True, "simulate": True})
    chosen = list(
        ydl.build_format_selector(format_selector)(
            {"formats": formats, "incomplete_formats": False}
        )
    )
    assert chosen, f"{format_selector!r} selected nothing"
    return chosen[0].get("requested_formats") or [chosen[0]]


def codecs(picked: list[dict]) -> tuple[str, str]:
    video = next((f for f in picked if f.get("vcodec", "none") != "none"), {})
    audio = next((f for f in picked if f.get("acodec", "none") != "none"), {})
    return video.get("vcodec", "none"), audio.get("acodec", "none")


# -- the presets do what they say -----------------------------------------


def test_best_quality_takes_the_highest_resolution():
    picked = select(preset("best")["format"], YOUTUBE_FORMATS)

    video, _audio = codecs(picked)
    assert video.startswith("av01"), "best quality should reach the 4K AV1 stream"
    assert picked[0]["height"] == 2160


def test_compatible_picks_h264_and_aac():
    picked = select(preset("compatible")["format"], YOUTUBE_FORMATS)

    video, audio = codecs(picked)
    assert video.startswith("avc1"), f"expected H.264, got {video}"
    assert audio.startswith("mp4a"), f"expected AAC, got {audio}"
    # YouTube caps H.264 at 1080p, so this is the ceiling, not a mistake.
    assert picked[0]["height"] == 1080


def test_compatible_720_caps_the_resolution():
    picked = select(preset("compatible-720")["format"], YOUTUBE_FORMATS)

    video, audio = codecs(picked)
    assert video.startswith("avc1")
    assert audio.startswith("mp4a")
    assert picked[0]["height"] == 720


@pytest.mark.parametrize("preset_id", [p["id"] for p in FORMAT_PRESETS])
def test_every_preset_still_selects_something_without_h264(preset_id):
    """The fallbacks matter: a video with no H.264 must still download."""
    picked = select(preset(preset_id)["format"], VP9_ONLY_FORMATS)

    assert picked, "a preset must never leave a video unselectable"


@pytest.mark.parametrize("preset_id", [p["id"] for p in FORMAT_PRESETS])
def test_every_preset_selector_is_valid(preset_id):
    ydl = yt_dlp.YoutubeDL({"quiet": True})
    assert ydl.build_format_selector(preset(preset_id)["format"]) is not None


# -- the preset list itself ------------------------------------------------


def test_presets_are_well_formed():
    ids = [p["id"] for p in FORMAT_PRESETS]

    assert len(ids) == len(set(ids)), "preset ids must be unique"
    assert CUSTOM not in ids, "'custom' is reserved for 'matches no preset'"
    for p in FORMAT_PRESETS:
        assert p["label"] and p["detail"]
        assert p["merge_output_format"] in {"mp4", "mkv", "webm"}


def test_the_shipped_default_is_a_preset():
    """Whatever a fresh install gets should be nameable in the dropdown."""
    defaults = DownloadDefaults()

    assert match_preset(defaults.format, defaults.merge_output_format) == "best"


def test_match_preset_needs_both_halves():
    compatible = preset("compatible")

    assert match_preset(compatible["format"], "mp4") == "compatible"
    # Right selector, wrong container: not that preset.
    assert match_preset(compatible["format"], "mkv") == CUSTOM
    assert match_preset("bestvideo+bestaudio", "mp4") == CUSTOM
    assert match_preset(None, None) == CUSTOM


def test_match_preset_tolerates_surrounding_whitespace():
    compatible = preset("compatible")

    assert match_preset(f"  {compatible['format']} ", " mp4 ") == "compatible"


def test_unknown_preset_id():
    assert preset("nope") is None
