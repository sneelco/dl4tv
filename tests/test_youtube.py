from __future__ import annotations

import pytest

from app.youtube import NotAuthenticated, YouTubeClient, parse_duration


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT4M13S", 253),
        ("PT1H2M3S", 3723),
        ("PT45S", 45),
        ("P1DT2H", 93600),
        ("PT0S", 0),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.youtube.com/playlist?list=PLabc123", "PLabc123"),
        ("https://www.youtube.com/watch?v=xyz&list=PLabc123&index=2", "PLabc123"),
        ("PLabc123def", "PLabc123def"),
        ("LL", "LL"),
        ("https://www.youtube.com/@SomeChannel", None),
        ("", None),
        ("just some words", None),
    ],
)
def test_extract_playlist_id(value, expected):
    assert YouTubeClient._extract_playlist_id(value) == expected


def test_channel_id_becomes_uploads_playlist():
    client = YouTubeClient(api_key="k")
    try:
        channel = "UCabcdefghijklmnopqrstuv"
        assert client._channel_uploads_id(channel) == "UUabcdefghijklmnopqrstuv"
        assert (
            client._channel_uploads_id(f"https://www.youtube.com/channel/{channel}")
            == f"UU{channel[2:]}"
        )
    finally:
        client.close()


def test_client_requires_credentials():
    with pytest.raises(NotAuthenticated):
        YouTubeClient()
