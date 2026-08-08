from __future__ import annotations

import pytest

from app.sources import effective_source, make_source
from app.youtube import NotAuthenticated, YouTubeClient, YouTubeError
from app.ytdlp import YtDlpSource


def entry(video_id: str, title: str | None = None, **extra) -> dict:
    return {"id": video_id, "title": title or f"Video {video_id}", "duration": 600, **extra}


def playlist(*entries, title: str = "Cooking Playlist", **extra) -> dict:
    return {"title": title, "entries": list(entries), **extra}


class FakeExtractor:
    """Stands in for yt-dlp; records the URLs it was asked for."""

    def __init__(self, responses: dict | None = None, default=None):
        self.responses = responses or {}
        self.default = default
        self.calls: list[tuple[str, int | None]] = []

    def __call__(self, url: str, limit: int | None = None):
        self.calls.append((url, limit))
        for fragment, payload in self.responses.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        return self.default


def test_lists_playlist_video_ids():
    extractor = FakeExtractor({"list=PL1": playlist(entry("a"), entry("b"), entry("c"))})
    source = YtDlpSource(extractor=extractor)

    assert source.playlist_video_ids("PL1") == ["a", "b", "c"]
    url, limit = extractor.calls[0]
    assert url == "https://www.youtube.com/playlist?list=PL1"
    assert limit is None


def test_video_details_reuse_the_playlist_fetch():
    extractor = FakeExtractor(
        {"list=PL1": playlist(entry("a", "First", duration=125.0), entry("b", "Second"))}
    )
    source = YtDlpSource(extractor=extractor)

    ids = source.playlist_video_ids("PL1")
    details = source.video_details(ids)

    # One network call total -- details come from the flat listing.
    assert len(extractor.calls) == 1
    assert details["a"].title == "First"
    assert details["a"].duration == 125
    assert details["b"].title == "Second"


def test_unavailable_entries_are_omitted_like_the_api():
    extractor = FakeExtractor(
        {
            "list=PL1": playlist(
                entry("ok"),
                entry("gone", "[Deleted video]", duration=None),
                entry("secret", "[Private video]", duration=None),
                entry("members", "Members only", availability="subscriber_only"),
            )
        }
    )
    source = YtDlpSource(extractor=extractor)

    ids = source.playlist_video_ids("PL1")
    details = source.video_details(ids)

    # All four are listed, but only the usable one has details -- the sync
    # engine then records the rest as unavailable.
    assert ids == ["ok", "gone", "secret", "members"]
    assert set(details) == {"ok"}


def test_live_and_upcoming_are_flagged():
    extractor = FakeExtractor(
        {
            "list=PL1": playlist(
                entry("live", live_status="is_live"),
                entry("soon", live_status="is_upcoming"),
                entry("done", live_status="was_live"),
            )
        }
    )
    source = YtDlpSource(extractor=extractor)

    details = source.video_details(source.playlist_video_ids("PL1"))

    assert details["live"].live_state == "live"
    assert details["soon"].live_state == "upcoming"
    assert details["done"].live_state is None


def test_duplicate_entries_are_collapsed():
    extractor = FakeExtractor({"list=PL1": playlist(entry("a"), entry("a"), entry("b"))})

    assert YtDlpSource(extractor=extractor).playlist_video_ids("PL1") == ["a", "b"]


def test_null_entries_are_skipped():
    # yt-dlp yields None for entries it had to skip under ignoreerrors.
    extractor = FakeExtractor({"list=PL1": {"title": "x", "entries": [entry("a"), None]}})

    assert YtDlpSource(extractor=extractor).playlist_video_ids("PL1") == ["a"]


def test_details_for_an_uncached_id_fall_back_to_a_direct_lookup():
    extractor = FakeExtractor({"watch?v=solo": entry("solo", "Standalone")})
    source = YtDlpSource(extractor=extractor)

    details = source.video_details(["solo"])

    assert details["solo"].title == "Standalone"
    assert extractor.calls == [("https://www.youtube.com/watch?v=solo", None)]


def test_resolve_playlist_from_url():
    extractor = FakeExtractor({"list=PL1": playlist(entry("a"), title="Cooking", playlist_count=12)})
    source = YtDlpSource(extractor=extractor)

    info = source.resolve_playlist("https://www.youtube.com/playlist?list=PL1")

    assert (info.id, info.title, info.item_count) == ("PL1", "Cooking", 12)
    # Resolving only needs the first page.
    assert extractor.calls[0][1] == 1


def test_resolve_channel_handle_to_uploads_playlist():
    extractor = FakeExtractor(
        {
            "@SomeChannel/videos": {"title": "Some Channel", "channel_id": "UCabc123", "entries": []},
            "list=UUabc123": playlist(entry("a"), title="Some Channel"),
        }
    )
    source = YtDlpSource(extractor=extractor)

    info = source.resolve_playlist("https://www.youtube.com/@SomeChannel")

    assert info.id == "UUabc123"


def test_resolve_channel_id_needs_no_lookup():
    channel = "UCabcdefghijklmnopqrstuv"
    extractor = FakeExtractor({f"list=UU{channel[2:]}": playlist(entry("a"))})
    source = YtDlpSource(extractor=extractor)

    assert source.resolve_playlist(channel).id == f"UU{channel[2:]}"


def test_resolve_rejects_nonsense():
    source = YtDlpSource(extractor=FakeExtractor())
    with pytest.raises(YouTubeError, match="Could not find a playlist"):
        source.resolve_playlist("just some words")


def test_extraction_failure_becomes_a_youtube_error():
    extractor = FakeExtractor({"list=PL1": RuntimeError("ERROR: unable to download page")})
    source = YtDlpSource(extractor=extractor)

    with pytest.raises(YouTubeError, match="unable to download page"):
        source.playlist_video_ids("PL1")


def test_empty_extraction_explains_private_playlists():
    source = YtDlpSource(extractor=FakeExtractor(default=None))

    with pytest.raises(YouTubeError, match="private"):
        source.playlist_video_ids("PL1")


def test_my_playlists_needs_an_account():
    source = YtDlpSource(extractor=FakeExtractor())

    with pytest.raises(NotAuthenticated, match="Google account"):
        source.my_playlists()


def test_cookies_are_passed_through():
    assert YtDlpSource(cookies_file="/config/cookies.txt").cookies_file == "/config/cookies.txt"


# -- source selection ------------------------------------------------------


def test_effective_source_auto_prefers_credentials(store):
    config = store.config

    assert effective_source(config, has_token=False) == "yt-dlp"
    assert effective_source(config, has_token=True) == "api"

    config.youtube.api_key = "key"
    assert effective_source(config, has_token=False) == "api"


def test_effective_source_respects_an_explicit_choice(store):
    config = store.config
    config.youtube.api_key = "key"
    config.youtube.source = "yt-dlp"

    assert effective_source(config, has_token=True) == "yt-dlp"

    config.youtube.source = "api"
    assert effective_source(config, has_token=False) == "api"


def test_make_source_without_credentials_uses_ytdlp(store):
    source = make_source(store)
    try:
        assert isinstance(source, YtDlpSource)
    finally:
        source.close()


def test_make_source_with_api_key_uses_the_api(store):
    store.update_config(lambda config: setattr(config.youtube, "api_key", "key"))

    source = make_source(store)
    try:
        assert isinstance(source, YouTubeClient)
    finally:
        source.close()


def test_make_source_passes_the_cookies_file(store):
    store.update_config(
        lambda config: setattr(config.downloads, "cookies_file", "/config/cookies.txt")
    )

    source = make_source(store)
    try:
        assert source.cookies_file == "/config/cookies.txt"
    finally:
        source.close()
