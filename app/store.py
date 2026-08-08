"""Flat-file persistence: config.yaml, state.json and the OAuth token.

Writes are atomic (temp file + ``os.replace``) and serialised behind a lock, so
a crash mid-write can never leave a half-written config behind.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig, AppState
from .settings import Env, env

log = logging.getLogger("dl4tv.store")

CONFIG_HEADER = """\
# dl4tv configuration.
# Edited by the web UI, but safe to edit by hand while the app is stopped.
# See the README for what each option does.
"""


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class Store:
    def __init__(self, environment: Env | None = None) -> None:
        self.env = environment or env()
        self._lock = threading.RLock()
        self._config: AppConfig | None = None
        self._state: AppState | None = None

    # -- config -----------------------------------------------------------

    @property
    def config(self) -> AppConfig:
        with self._lock:
            if self._config is None:
                self._config = self._read_config()
            return self._config

    def _read_config(self) -> AppConfig:
        path = self.env.config_file
        if not path.exists():
            config = AppConfig()
            self._write_config(config)
            log.info("created default config at %s", path)
            return config
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return AppConfig.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - never start with a broken config
            backup = path.with_suffix(path.suffix + ".invalid")
            shutil.copy2(path, backup)
            log.error("config at %s is invalid (%s); backed up to %s and starting "
                      "from defaults", path, exc, backup)
            config = AppConfig()
            self._write_config(config)
            return config

    def _write_config(self, config: AppConfig) -> None:
        body = yaml.safe_dump(
            config.model_dump(mode="json"), sort_keys=False, allow_unicode=True
        )
        _atomic_write(self.env.config_file, CONFIG_HEADER + body)

    def save_config(self, config: AppConfig) -> AppConfig:
        with self._lock:
            self._write_config(config)
            self._config = config
            return config

    def update_config(self, mutate: Callable[[AppConfig], Any]) -> AppConfig:
        """Mutate the config under the lock and persist the result."""
        with self._lock:
            config = self.config.model_copy(deep=True)
            mutate(config)
            return self.save_config(config)

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> AppState:
        with self._lock:
            if self._state is None:
                self._state = self._read_state()
            return self._state

    def _read_state(self) -> AppState:
        path = self.env.state_file
        if not path.exists():
            return AppState()
        try:
            return AppState.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            backup = path.with_suffix(path.suffix + ".invalid")
            shutil.copy2(path, backup)
            log.error("state at %s is unreadable (%s); backed up to %s and starting "
                      "fresh -- already-downloaded videos may be fetched again",
                      path, exc, backup)
            return AppState()

    def save_state(self, state: AppState | None = None) -> AppState:
        with self._lock:
            if state is not None:
                self._state = state
            current = self.state
            _atomic_write(
                self.env.state_file,
                json.dumps(current.model_dump(mode="json"), indent=2),
            )
            return current

    def update_state(self, mutate: Callable[[AppState], Any]) -> AppState:
        with self._lock:
            state = self.state
            mutate(state)
            return self.save_state(state)

    # -- oauth token ------------------------------------------------------

    def read_token(self) -> dict | None:
        path = self.env.token_file
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.error("could not read %s: %s", path, exc)
            return None

    def write_token(self, payload: dict | str) -> None:
        data = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
        with self._lock:
            _atomic_write(self.env.token_file, data)
            try:
                os.chmod(self.env.token_file, 0o600)
            except OSError:
                pass

    def clear_token(self) -> None:
        with self._lock:
            self.env.token_file.unlink(missing_ok=True)

    # -- paths ------------------------------------------------------------

    def resolve_folder(self, folder: str) -> Path:
        """Resolve a mapping folder against the download root."""
        candidate = Path(folder).expanduser()
        if not candidate.is_absolute():
            candidate = self.env.download_dir / candidate
        return candidate


_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def set_store(store: Store | None) -> None:
    """Test hook: swap in (or reset) the process-wide store."""
    global _store
    with _store_lock:
        _store = store
