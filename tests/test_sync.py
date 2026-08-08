from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import sync as sync_module
from app.downloader import DownloadOutcome
from app.models import Mapping, Schedule, VideoRecord
from app.sync import SyncManager, compute_next_run, needs_attempt
from app.youtube import VideoInfo


class FakeClient:
    def __init__(self, videos: list[VideoInfo], missing: list[str] | None = None):
        self.videos = {v.id: v for v in videos}
        self.missing = set(missing or [])
        self.closed = False

    def playlist_video_ids(self, playlist_id, cap=None):
        return list(self.videos) + sorted(self.missing)

    def video_details(self, ids):
        return {i: self.videos[i] for i in ids if i in self.videos}

    def close(self):
        self.closed = True


def video(video_id: str, title: str = "", duration: int = 600, live=None) -> VideoInfo:
    return VideoInfo(
        id=video_id,
        title=title or f"Video {video_id}",
        duration=duration,
        live_state=live,
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def wire(store, monkeypatch):
    """Install a mapping plus fakes for the YouTube client and the downloader."""

    def setup(videos, outcomes=None, missing=None, **mapping_kwargs):
        mapping_kwargs.setdefault("folder", "cooking")
        mapping = Mapping(playlist_id="PL1", title="Cooking", **mapping_kwargs)
        store.update_config(lambda config: config.mappings.append(mapping))
        client = FakeClient(videos, missing)
        monkeypatch.setattr(sync_module, "make_source", lambda _store: client)

        calls: list[str] = []

        def fake_download(video_id, destination, defaults, mapping=None, hook=None, info=None):
            calls.append(video_id)
            outcome = (outcomes or {}).get(video_id)
            if outcome is None:
                path = destination / f"{video_id}.mkv"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")
                return DownloadOutcome(ok=True, path=str(path), filesize=5)
            return outcome

        monkeypatch.setattr(sync_module, "download_video", fake_download)
        return mapping, calls

    return setup


async def test_downloads_new_videos_once(store, wire):
    mapping, calls = wire([video("a"), video("b")])
    manager = SyncManager(store)

    run = await manager.run()

    assert calls == ["a", "b"]
    assert (run.downloaded, run.failed, run.skipped) == (2, 0, 0)
    playlist = store.state.playlist(mapping.id)
    assert playlist.last_status == "ok"
    assert playlist.videos["a"].status == "downloaded"
    assert playlist.videos["a"].path.endswith("cooking/a.mkv")

    # A second run finds nothing new.
    calls.clear()
    second = await manager.run()
    assert calls == []
    assert second.downloaded == 0


async def test_permanent_failure_is_not_retried(store, wire):
    outcome = DownloadOutcome(
        ok=False, error="This video is DRM protected", error_kind="drm", permanent=True
    )
    mapping, calls = wire([video("a")], outcomes={"a": outcome})
    manager = SyncManager(store)

    run = await manager.run()

    assert run.failed == 1
    record = store.state.playlist(mapping.id).videos["a"]
    assert record.permanent and record.error_kind == "drm"

    calls.clear()
    await manager.run()
    assert calls == []


async def test_transient_failure_retries_until_attempts_exhausted(store, wire):
    outcome = DownloadOutcome(
        ok=False, error="read operation timed out", error_kind="network", permanent=False
    )
    mapping, calls = wire([video("a")], outcomes={"a": outcome})
    store.update_config(lambda config: setattr(config.downloads, "max_attempts", 2))
    manager = SyncManager(store)

    await manager.run()
    assert store.state.playlist(mapping.id).videos["a"].permanent is False

    await manager.run()
    record = store.state.playlist(mapping.id).videos["a"]
    assert record.attempts == 2
    assert record.permanent is True

    calls.clear()
    await manager.run()
    assert calls == []


async def test_short_videos_are_filtered(store, wire):
    mapping, calls = wire(
        [video("short", duration=30), video("long", duration=900)],
        min_duration_seconds=61,
    )
    manager = SyncManager(store)

    run = await manager.run()

    assert calls == ["long"]
    assert run.skipped == 1
    record = store.state.playlist(mapping.id).videos["short"]
    assert record.status == "skipped" and record.permanent
    assert "shorter than" in record.reason


async def test_live_video_is_skipped_but_reconsidered_later(store, wire, monkeypatch):
    mapping, calls = wire([video("a", live="upcoming")])
    manager = SyncManager(store)

    await manager.run()
    assert calls == []
    record = store.state.playlist(mapping.id).videos["a"]
    assert record.status == "skipped" and not record.permanent

    # The premiere has happened: the same video now downloads.
    client = FakeClient([video("a")])
    monkeypatch.setattr(sync_module, "make_source", lambda _store: client)
    await manager.run()
    assert calls == ["a"]


async def test_video_missing_from_api_is_marked_unavailable(store, wire):
    mapping, calls = wire([video("a")], missing=["gone"])
    manager = SyncManager(store)

    run = await manager.run()

    assert calls == ["a"]
    assert run.failed == 1
    record = store.state.playlist(mapping.id).videos["gone"]
    assert record.error_kind == "unavailable" and record.permanent


async def test_max_new_per_run_limits_downloads(store, wire):
    _mapping, calls = wire(
        [video("a"), video("b"), video("c")], max_new_per_run=2
    )
    manager = SyncManager(store)

    await manager.run()
    assert calls == ["a", "b"]

    calls.clear()
    await manager.run()
    assert calls == ["c"]


async def test_playlist_error_does_not_abort_the_run(store, wire, monkeypatch):
    mapping, _calls = wire([video("a")])

    class Broken(FakeClient):
        def playlist_video_ids(self, playlist_id, cap=None):
            raise RuntimeError("API exploded")

    monkeypatch.setattr(sync_module, "make_source", lambda _store: Broken([]))
    manager = SyncManager(store)

    run = await manager.run()

    assert run.status == "error"
    assert "API exploded" in (run.error or "")
    assert store.state.playlist(mapping.id).last_status == "error"


async def test_disabled_mappings_are_skipped_unless_named(store, wire):
    mapping, calls = wire([video("a")], enabled=False)
    manager = SyncManager(store)

    await manager.run()
    assert calls == []

    await manager.run(mapping_ids=[mapping.id])
    assert calls == ["a"]


async def test_run_history_is_capped(store, wire):
    wire([])
    manager = SyncManager(store)
    for _ in range(3):
        await manager.run()

    runs = store.state.runs
    assert len(runs) == 3
    assert all(run.finished_at is not None for run in runs)
    assert runs[0].started_at >= runs[-1].started_at


# -- scheduling ------------------------------------------------------------


def test_compute_next_run_interval():
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    schedule = Schedule(mode="interval", interval_minutes=90)

    assert compute_next_run(schedule, now) == now + timedelta(minutes=90)


def test_compute_next_run_daily_rolls_to_tomorrow():
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)

    today = compute_next_run(Schedule(mode="daily", daily_at="23:30"), now)
    tomorrow = compute_next_run(Schedule(mode="daily", daily_at="03:00"), now)

    assert today == datetime(2024, 5, 1, 23, 30, tzinfo=UTC)
    assert tomorrow == datetime(2024, 5, 2, 3, 0, tzinfo=UTC)


def test_compute_next_run_honours_timezone():
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    schedule = Schedule(mode="daily", daily_at="03:00", timezone="America/New_York")

    # 03:00 EDT on 2 May is 07:00 UTC.
    assert compute_next_run(schedule, now) == datetime(2024, 5, 2, 7, 0, tzinfo=UTC)


def test_compute_next_run_survives_bad_input():
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)

    assert compute_next_run(Schedule(enabled=False), now) is None
    assert compute_next_run(Schedule(daily_at="nope"), now) == datetime(
        2024, 5, 2, 3, 0, tzinfo=UTC
    )
    assert compute_next_run(Schedule(timezone="Mars/Olympus"), now).tzinfo is not None


# -- candidate selection ---------------------------------------------------


def test_needs_attempt():
    exists = lambda _path: True  # noqa: E731
    missing = lambda _path: False  # noqa: E731

    assert needs_attempt(None, 3, exists) is True
    assert needs_attempt(VideoRecord(video_id="a", status="downloaded", path="/x"), 3, exists) is False
    # The file was deleted from disk, so fetch it again.
    assert needs_attempt(VideoRecord(video_id="a", status="downloaded", path="/x"), 3, missing) is True
    assert needs_attempt(VideoRecord(video_id="a", status="failed", permanent=True), 3, exists) is False
    assert needs_attempt(VideoRecord(video_id="a", status="failed", attempts=1), 3, exists) is True
    assert needs_attempt(VideoRecord(video_id="a", status="failed", attempts=3), 3, exists) is False


async def test_missing_ffmpeg_does_not_consume_retries(store, wire):
    """An install problem must not permanently write off a whole playlist."""
    outcome = DownloadOutcome(
        ok=False,
        error="You have requested merging of multiple formats but ffmpeg is not installed",
        error_kind="no_ffmpeg",
        permanent=False,
    )
    mapping, calls = wire([video("a")], outcomes={"a": outcome})
    store.update_config(lambda config: setattr(config.downloads, "max_attempts", 2))
    manager = SyncManager(store)

    for _ in range(4):
        await manager.run()

    record = store.state.playlist(mapping.id).videos["a"]
    assert record.attempts == 0
    assert record.permanent is False
    # Still being tried on every run, unlike a normal transient failure.
    assert calls == ["a", "a", "a", "a"]


async def test_folder_is_created_even_with_nothing_to_download(store, wire, env):
    """A newly mapped playlist should appear in the library right away."""
    wire([])
    assert not (env.download_dir / "cooking").exists()

    await SyncManager(store).run()

    assert (env.download_dir / "cooking").is_dir()


async def test_unwritable_folder_is_reported_not_crashed(store, wire, env):
    # A file where the parent folder should be: mkdir cannot succeed.
    (env.download_dir / "cooking").write_text("not a directory")
    mapping, calls = wire([video("a")], folder="cooking/nested")
    manager = SyncManager(store)

    run = await manager.run()

    assert calls == []
    assert run.status == "error"
    assert "Could not create download folder" in (run.error or "")
    assert store.state.playlist(mapping.id).last_status == "error"
