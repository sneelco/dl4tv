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

DEFAULT_PORT = 8484


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_port(warnings: list[str]) -> int:
    """Work out the listen port, tolerating Kubernetes service-link variables.

    kubelet injects ``<SERVICE>_PORT=tcp://10.43.0.1:8484`` for every Service in
    the namespace, so a Service named ``dl4tv`` overwrites DL4TV_PORT with a URL.
    That is the platform talking, not the operator, so ignore it rather than
    crash -- and offer DL4TV_HTTP_PORT, which nothing else claims.
    """
    explicit = (os.environ.get("DL4TV_HTTP_PORT") or "").strip()
    if explicit:
        try:
            return int(explicit)
        except ValueError:
            warnings.append(
                f"DL4TV_HTTP_PORT={explicit!r} is not a number; using {DEFAULT_PORT}."
            )
            return DEFAULT_PORT

    raw = (os.environ.get("DL4TV_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        warnings.append(
            f"Ignoring DL4TV_PORT={raw!r} -- that looks like a Kubernetes service "
            f"link, not a port, so listening on {DEFAULT_PORT} instead. To choose a "
            "port here, use DL4TV_HTTP_PORT; to stop the injection entirely, set "
            "enableServiceLinks: false on the pod or rename the Service."
        )
        return DEFAULT_PORT


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
    # Problems found while reading the environment, reported once logging is up.
    warnings: tuple[str, ...] = ()

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
    warnings: list[str] = []
    config_dir = Path(os.environ.get("DL4TV_CONFIG_DIR", "/config")).expanduser()
    download_dir = Path(os.environ.get("DL4TV_DOWNLOAD_DIR", "/downloads")).expanduser()
    public_url = os.environ.get("DL4TV_PUBLIC_URL") or None
    return Env(
        config_dir=config_dir,
        download_dir=download_dir,
        host=os.environ.get("DL4TV_HOST", "0.0.0.0"),
        port=_resolve_port(warnings),
        public_url=public_url.rstrip("/") if public_url else None,
        passphrase=os.environ.get("DL4TV_PASSPHRASE") or None,
        log_level=os.environ.get("DL4TV_LOG_LEVEL", "INFO").upper(),
        # Google's OAuth library refuses plain-http redirect URIs unless told
        # otherwise. Self-hosted installs are usually http on a LAN.
        insecure_oauth_transport=_env_bool("DL4TV_OAUTH_INSECURE_TRANSPORT", True),
        warnings=tuple(warnings),
    )
