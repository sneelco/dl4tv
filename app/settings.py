"""Process-level settings, sourced from the environment.

Everything the user can change at runtime lives in ``config.yaml`` instead;
this module only covers the things that must be known before the app can read
its own configuration (where that configuration lives, mostly).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Env:
    config_dir: Path
    download_dir: Path
    host: str
    port: int
    public_url: str | None
    # Locks the UI before first boot; the UI cannot change or clear it.
    passphrase: str | None
    log_level: str
    insecure_oauth_transport: bool

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def state_file(self) -> Path:
        return self.config_dir / "state.json"

    @property
    def token_file(self) -> Path:
        return self.config_dir / "token.json"

    @property
    def session_key_file(self) -> Path:
        return self.config_dir / "session.key"


@lru_cache(maxsize=1)
def env() -> Env:
    config_dir = Path(os.environ.get("DL4TV_CONFIG_DIR", "/config")).expanduser()
    download_dir = Path(os.environ.get("DL4TV_DOWNLOAD_DIR", "/downloads")).expanduser()
    public_url = os.environ.get("DL4TV_PUBLIC_URL") or None
    return Env(
        config_dir=config_dir,
        download_dir=download_dir,
        host=os.environ.get("DL4TV_HOST", "0.0.0.0"),
        port=int(os.environ.get("DL4TV_PORT", "8484")),
        public_url=public_url.rstrip("/") if public_url else None,
        passphrase=os.environ.get("DL4TV_PASSPHRASE") or None,
        log_level=os.environ.get("DL4TV_LOG_LEVEL", "INFO").upper(),
        # Google's OAuth library refuses plain-http redirect URIs unless told
        # otherwise. Self-hosted installs are usually http on a LAN.
        insecure_oauth_transport=_env_bool("DL4TV_OAUTH_INSECURE_TRANSPORT", True),
    )
