"""The sync engine: work out which playlist videos are new, download them and
record what happened.

One run at a time, always. Runs can be triggered by the scheduler, by the
"Sync now" button, or on startup.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .auth import make_client
from .downloader import download_video
from .models import (
    AppConfig,
    Mapping,
    RunRecord,
    Schedule,
    VideoRecord,
    utcnow,
)
from .store import Store
from .youtube import VideoInfo, YouTubeError

log = logging.getLogger("dl4tv.sync")


class SyncCancelled(Exception):
    pass


def compute_next_run(schedule: Schedule, now: datetime | None = None) -> datetime | None:
    """Next scheduled run, in UTC, or None when scheduling is off."""
    if not schedule.enabled:
        return None
    now = now or utcnow()
    if schedule.mode == "interval":
        return now + timedelta(minutes=max(schedule.interval_minutes, 5))
    try:
        tz = ZoneInfo(schedule.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r; falling back to UTC", schedule.timezone)
        tz = UTC
    try:
        hour, minute = (int(part) for part in schedule.daily_at.split(":", 1))
    except ValueError:
        log.warning("invalid daily_at %r; falling back to 03:00", schedule.daily_at)
        hour, minute = 3, 0
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def needs_attempt(record: VideoRecord | None, max_attempts: int, folder_check: Callable[[str], bool]) -> bool:
    """Should this video be (re)downloaded on this run?"""
    if record is None:
        return True
    if record.permanent:
        return False
    if record.status == "downloaded":
        # Re-download if the file went missing (deleted or moved by hand).
        return not (record.path and folder_check(record.path))
    if record.status == "failed" and record.attempts >= max_attempts:
        return False
    return True


class SyncManager:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self.running = False
        self.progress: dict = {}
        self.next_run_at: datetime | None = None

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        config = self.store.config
        self.next_run_at = compute_next_run(config.schedule)
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        if config.schedule.run_on_start:
            self._task = asyncio.create_task(self.run(trigger="startup"))

    async def stop(self) -> None:
        self._cancel.set()
        for task in (self._scheduler_task, self._task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    def reschedule(self) -> None:
        """Recompute the next run after a settings change."""
        self.next_run_at = compute_next_run(self.store.config.schedule)
        self._wake.set()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except TimeoutError:
                pass
            self._wake.clear()
            schedule = self.store.config.schedule
            if not schedule.enabled:
                self.next_run_at = None
                continue
            if self.next_run_at is None:
                self.next_run_at = compute_next_run(schedule)
                continue
            if utcnow() >= self.next_run_at and not self.running:
                self.next_run_at = compute_next_run(schedule)
                self._task = asyncio.create_task(self.run(trigger="schedule"))

    # -- triggering -------------------------------------------------------

    def trigger(self, mapping_ids: list[str] | None = None, trigger: str = "manual") -> bool:
        """Kick off a run in the background. False if one is already going."""
        if self.running:
            return False
        self._task = asyncio.create_task(self.run(mapping_ids=mapping_ids, trigger=trigger))
        return True

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel.set()
        return True

    # -- the run itself ---------------------------------------------------

    async def run(
        self, mapping_ids: list[str] | None = None, trigger: str = "manual"
    ) -> RunRecord:
        async with self._lock:
            self.running = True
            self._cancel.clear()
            config = self.store.config
            selected = [
                m
                for m in config.mappings
                if (mapping_ids is None and m.enabled) or (mapping_ids and m.id in mapping_ids)
            ]
            record = RunRecord(trigger=trigger, mappings=len(selected))  # type: ignore[arg-type]
            log.info(
                "sync run started (%s) for %d playlist(s)", trigger, len(selected)
            )
            self._record_run(record)
            try:
                for mapping in selected:
                    if self._cancel.is_set():
                        raise SyncCancelled()
                    try:
                        await self._sync_mapping(config, mapping, record)
                    except SyncCancelled:
                        raise
                    except YouTubeError as exc:
                        record.error = record.error or str(exc)
                        self._finish_playlist(mapping, "error", str(exc))
                        log.error("playlist %s failed: %s", mapping.title, exc)
                    except Exception as exc:  # noqa: BLE001 - one bad playlist must
                        # not take down the whole run
                        record.error = record.error or str(exc)
                        self._finish_playlist(mapping, "error", str(exc))
                        log.exception("unexpected failure syncing %s", mapping.title)
                record.status = "partial" if record.failed else "ok"
                if record.error and not record.downloaded:
                    record.status = "error"
            except SyncCancelled:
                record.status = "cancelled"
                log.warning("sync run cancelled")
            finally:
                record.finished_at = utcnow()
                self._record_run(record)
                self.running = False
                self.progress = {}
                self._cancel.clear()
            log.info(
                "sync run finished: %d downloaded, %d failed, %d skipped",
                record.downloaded,
                record.failed,
                record.skipped,
            )
            return record

    async def _sync_mapping(
        self, config: AppConfig, mapping: Mapping, run: RunRecord
    ) -> None:
        defaults = config.downloads
        destination = self.store.resolve_folder(mapping.folder)
        log.info("syncing %r -> %s", mapping.title, destination)

        client = await asyncio.to_thread(make_client, self.store)
        try:
            video_ids = await asyncio.to_thread(client.playlist_video_ids, mapping.playlist_id)
            playlist_state = self.store.state.playlist(mapping.id)
            playlist_state.playlist_id = mapping.playlist_id

            candidates = [
                video_id
                for video_id in video_ids
                if needs_attempt(
                    playlist_state.videos.get(video_id),
                    defaults.max_attempts,
                    _file_exists,
                )
            ]
            log.info(
                "%r: %d video(s) in playlist, %d to consider",
                mapping.title,
                len(video_ids),
                len(candidates),
            )
            if not candidates:
                self._finish_playlist(mapping, "ok", None)
                return

            details = await asyncio.to_thread(client.video_details, candidates)
        finally:
            await asyncio.to_thread(client.close)

        to_download = self._filter_candidates(mapping, candidates, details, run)

        limit = mapping.max_new_per_run or defaults.max_new_per_run
        if limit and len(to_download) > limit:
            log.info(
                "%r: limiting this run to %d of %d new video(s)",
                mapping.title,
                limit,
                len(to_download),
            )
            to_download = to_download[:limit]

        errors: list[str] = []
        for index, video_id in enumerate(to_download, start=1):
            if self._cancel.is_set():
                raise SyncCancelled()
            info = details.get(video_id)
            title = info.title if info else video_id
            self.progress = {
                "mapping_id": mapping.id,
                "mapping_title": mapping.title,
                "video_id": video_id,
                "video_title": title,
                "index": index,
                "total": len(to_download),
                "percent": 0.0,
            }
            log.info("downloading [%d/%d] %s", index, len(to_download), title)
            outcome = await asyncio.to_thread(
                download_video,
                video_id,
                destination,
                defaults,
                mapping,
                self._progress_hook,
                info,
            )
            if self._cancel.is_set():
                raise SyncCancelled()

            record = playlist_state.videos.get(video_id) or VideoRecord(
                video_id=video_id, title=title
            )
            record.title = title or record.title
            record.attempts += 1
            record.last_attempt_at = utcnow()
            if info:
                record.duration = info.duration
                record.published_at = info.published_at
            if outcome.ok:
                record.status = "downloaded"
                record.path = outcome.path
                record.filesize = outcome.filesize
                record.downloaded_at = utcnow()
                record.error = None
                record.error_kind = None
                record.permanent = False
                run.downloaded += 1
                log.info("downloaded %s -> %s", title, outcome.path)
            else:
                record.status = "failed"
                record.error = outcome.error
                record.error_kind = outcome.error_kind
                record.permanent = outcome.permanent or (
                    record.attempts >= defaults.max_attempts
                )
                run.failed += 1
                errors.append(f"{title}: {outcome.error}")
                log.error(
                    "failed %s (%s%s): %s",
                    title,
                    outcome.error_kind,
                    ", giving up" if record.permanent else ", will retry",
                    outcome.error,
                )
            playlist_state.videos[video_id] = record
            self.store.save_state()

        self._finish_playlist(
            mapping, "partial" if errors else "ok", errors[0] if errors else None
        )

    def _filter_candidates(
        self,
        mapping: Mapping,
        candidates: Iterable[str],
        details: dict[str, VideoInfo],
        run: RunRecord,
    ) -> list[str]:
        """Drop videos we should not download, recording why."""
        playlist_state = self.store.state.playlist(mapping.id)
        keep: list[str] = []
        for video_id in candidates:
            info = details.get(video_id)
            record = playlist_state.videos.get(video_id) or VideoRecord(
                video_id=video_id, title=video_id
            )
            if info is None:
                # videos.list silently omits private and deleted videos.
                record.status = "failed"
                record.error = (
                    "The API did not return this video -- it is private, deleted, "
                    "or otherwise unavailable."
                )
                record.error_kind = "unavailable"
                record.permanent = True
                record.last_attempt_at = utcnow()
                playlist_state.videos[video_id] = record
                run.failed += 1
                continue

            record.title = info.title
            record.duration = info.duration
            record.published_at = info.published_at

            if info.live_state:
                # Re-checked on the next run; a premiere becomes a normal video.
                record.status = "skipped"
                record.reason = f"Skipped: video is {info.live_state}"
                record.permanent = False
                playlist_state.videos[video_id] = record
                run.skipped += 1
                continue

            duration = info.duration or 0
            if mapping.min_duration_seconds and duration < mapping.min_duration_seconds:
                record.status = "skipped"
                record.reason = (
                    f"Skipped: {duration}s is shorter than the {mapping.min_duration_seconds}s minimum"
                )
                record.permanent = True
                playlist_state.videos[video_id] = record
                run.skipped += 1
                continue
            if mapping.max_duration_seconds and duration > mapping.max_duration_seconds:
                record.status = "skipped"
                record.reason = (
                    f"Skipped: {duration}s is longer than the {mapping.max_duration_seconds}s maximum"
                )
                record.permanent = True
                playlist_state.videos[video_id] = record
                run.skipped += 1
                continue

            record.reason = None
            keep.append(video_id)
        self.store.save_state()
        return keep

    # -- helpers ----------------------------------------------------------

    def _progress_hook(self, payload: dict) -> None:
        if self._cancel.is_set():
            raise SyncCancelled("cancelled by user")
        if payload.get("status") != "downloading" or not self.progress:
            return
        total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
        downloaded = payload.get("downloaded_bytes") or 0
        if total:
            self.progress["percent"] = round(downloaded / total * 100, 1)
        self.progress["speed"] = payload.get("speed")
        self.progress["eta"] = payload.get("eta")

    def _finish_playlist(self, mapping: Mapping, status: str, error: str | None) -> None:
        def mutate(state):
            playlist = state.playlist(mapping.id)
            playlist.playlist_id = mapping.playlist_id
            playlist.last_sync_at = utcnow()
            playlist.last_status = status
            playlist.last_error = error

        self.store.update_state(mutate)

    def _record_run(self, record: RunRecord) -> None:
        def mutate(state):
            for index, existing in enumerate(state.runs):
                if existing.id == record.id:
                    state.runs[index] = record
                    break
            else:
                state.runs.insert(0, record)
            del state.runs[25:]

        self.store.update_state(mutate)


def _file_exists(path: str) -> bool:
    from pathlib import Path  # noqa: PLC0415

    try:
        return Path(path).exists()
    except OSError:
        return False


_manager: SyncManager | None = None


def get_manager(store: Store | None = None) -> SyncManager:
    global _manager
    if _manager is None:
        from .store import get_store  # noqa: PLC0415

        _manager = SyncManager(store or get_store())
    return _manager


def set_manager(manager: SyncManager | None) -> None:
    global _manager
    _manager = manager
