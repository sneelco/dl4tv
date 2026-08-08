"""Optional passphrase lock for the UI.

dl4tv is open by default. Setting a passphrase — in Settings, or via
``DL4TV_PASSPHRASE`` — turns on a single shared-secret gate: one passphrase, no
usernames, a signed cookie once you are in.

Only a scrypt hash of the passphrase is ever written to ``config.yaml``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from .models import AppConfig
from .settings import Env

log = logging.getLogger("dl4tv.security")

# scrypt parameters: ~16 MB and a few tens of ms per check, which is plenty
# against offline cracking while staying comfortable on a NAS.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

SESSION_COOKIE = "dl4tv_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600


# --------------------------------------------------------------------------
# passphrase hashing
# --------------------------------------------------------------------------


def hash_passphrase(passphrase: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_passphrase(passphrase: str, stored: str | None) -> bool:
    if not stored or not passphrase:
        return False
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        candidate = hashlib.scrypt(
            passphrase.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError) as exc:
        log.error("stored passphrase hash is unreadable: %s", exc)
        return False
    return hmac.compare_digest(candidate, expected)


# --------------------------------------------------------------------------
# lock state
# --------------------------------------------------------------------------


def is_locked(config: AppConfig, env: Env) -> bool:
    return bool(env.passphrase or config.security.passphrase_hash)


def managed_by_env(env: Env) -> bool:
    """An env-set passphrase cannot be changed from the UI."""
    return bool(env.passphrase)


def check_passphrase(config: AppConfig, env: Env, candidate: str) -> bool:
    if not candidate:
        return False
    if env.passphrase:
        return hmac.compare_digest(candidate, env.passphrase)
    return verify_passphrase(candidate, config.security.passphrase_hash)


def credential_fingerprint(config: AppConfig, env: Env) -> str:
    """Changes whenever the passphrase changes, which expires old sessions."""
    source = env.passphrase or config.security.passphrase_hash or ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# session tokens
# --------------------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_token(secret: bytes, fingerprint: str, ttl: int = SESSION_TTL_SECONDS) -> str:
    payload = {"exp": int(time.time()) + ttl, "fp": fingerprint}
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}"


def verify_token(token: str | None, secret: bytes, fingerprint: str) -> bool:
    if not token or "." not in token:
        return False
    body, _, signature = token.partition(".")
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_b64decode(signature), expected):
            return False
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError):
        return False
    if payload.get("fp") != fingerprint:
        # The passphrase changed since this session started.
        return False
    return int(payload.get("exp", 0)) > time.time()


def load_or_create_secret(path) -> bytes:
    """The key used to sign session cookies, persisted so restarts keep you in."""
    try:
        if path.exists():
            raw = path.read_bytes().strip()
            if len(raw) >= 32:
                return raw
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
    secret = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secret)
        os.chmod(path, 0o600)
    except OSError as exc:
        # Not fatal: sessions simply will not survive a restart.
        log.warning("could not persist the session key to %s: %s", path, exc)
    return secret
