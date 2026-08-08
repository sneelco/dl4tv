"""In-memory ring buffer of recent log records, surfaced in the UI."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime

_MAX_RECORDS = 500


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = _MAX_RECORDS) -> None:
        super().__init__()
        self._records: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._counter = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{self.format(record)}"
        except Exception:  # noqa: BLE001 - logging must never raise
            message = record.msg if isinstance(record.msg, str) else "<unformattable>"
        with self._lock:
            self._counter += 1
            self._records.append(
                {
                    "seq": self._counter,
                    "ts": datetime.fromtimestamp(
                        record.created, tz=UTC
                    ).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
            )

    def records(self, since: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            items = [r for r in self._records if r["seq"] > since]
        return items[-limit:]


handler = RingBufferHandler()


def install(level: str = "INFO") -> RingBufferHandler:
    logger = logging.getLogger("dl4tv")
    logger.setLevel(level)
    if handler not in logger.handlers:
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
    return handler
