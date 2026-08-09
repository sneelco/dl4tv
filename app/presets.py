"""Ready-made download format choices.

yt-dlp's format selector is powerful and completely opaque, and the wrong
choice produces files a TV silently refuses to play. These presets cover the
decision that actually matters -- quality versus what your player can decode --
and fill in the raw fields, which stay editable.

They are plain data: nothing extra is stored in ``config.yaml``, and the
selected preset is derived by matching the saved format and container.
"""

from __future__ import annotations

# YouTube offers H.264 (avc1) only up to 1080p; 1440p and 4K are VP9 or AV1
# only. So "most compatible" necessarily means "no more than 1080p".
FORMAT_PRESETS: list[dict[str, str]] = [
    {
        "id": "best",
        "label": "Best quality",
        "detail": (
            "Highest resolution available, usually VP9 or AV1. Smaller files for "
            "the quality, but many TVs and set-top boxes cannot decode them."
        ),
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mkv",
    },
    {
        "id": "compatible",
        "label": "Most compatible — H.264 + AAC in mp4",
        "detail": (
            "Plays on essentially anything, and lets ErsatzTV copy the video "
            "stream instead of transcoding it. Capped at 1080p, because YouTube "
            "does not offer H.264 above that. A video with no H.264 at all fails "
            "rather than producing an mp4 your player cannot decode."
        ),
        # Every branch stays H.264-in-mp4 on purpose. An earlier version fell
        # back to bestvideo+bestaudio (any codec) while still forcing an mp4
        # container, and yt-dlp will happily write VP9 or AV1 into a .mp4 -- a
        # file that looks correct and plays nowhere. Better to fail visibly on
        # the rare video with no H.264 than to hand ErsatzTV a mislabelled one.
        "format": (
            "bestvideo[vcodec~='^(h264|avc)'][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec~='^(h264|avc)']+bestaudio[acodec~='^(mp4a|aac)']/"
            "best[ext=mp4]"
        ),
        "merge_output_format": "mp4",
    },
    {
        "id": "compatible-720",
        "label": "Most compatible, 720p — for older or fussier players",
        "detail": (
            "As above but capped at 720p, for hardware that struggles with 1080p "
            "or high bitrates."
        ),
        "format": (
            "bestvideo[vcodec~='^(h264|avc)'][height<=720][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec~='^(h264|avc)'][height<=720]+bestaudio[acodec~='^(mp4a|aac)']/"
            "best[ext=mp4][height<=720]/best[ext=mp4]"
        ),
        "merge_output_format": "mp4",
    },
]

CUSTOM = "custom"


def match_preset(format_selector: str | None, merge_output_format: str | None) -> str:
    """Which preset the saved settings correspond to, or ``custom``."""
    for preset in FORMAT_PRESETS:
        if (
            preset["format"] == (format_selector or "").strip()
            and preset["merge_output_format"] == (merge_output_format or "").strip()
        ):
            return preset["id"]
    return CUSTOM


def preset(preset_id: str) -> dict[str, str] | None:
    return next((p for p in FORMAT_PRESETS if p["id"] == preset_id), None)
