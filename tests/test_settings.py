from __future__ import annotations

import pytest

from app import settings as settings_module
from app.settings import DEFAULT_PORT, env


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("DL4TV_PORT", "DL4TV_HTTP_PORT", "DL4TV_HOST", "DL4TV_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    settings_module.env.cache_clear()
    yield monkeypatch
    settings_module.env.cache_clear()


def test_defaults(clean_env):
    assert env().port == DEFAULT_PORT
    assert env().warnings == ()


def test_plain_port(clean_env):
    clean_env.setenv("DL4TV_PORT", "9999")

    assert env().port == 9999
    assert env().warnings == ()


def test_kubernetes_service_link_does_not_crash(clean_env):
    """kubelet sets <SERVICE>_PORT=tcp://ip:port for a Service named dl4tv."""
    clean_env.setenv("DL4TV_PORT", "tcp://10.43.211.198:8484")

    settings = env()

    assert settings.port == DEFAULT_PORT
    assert len(settings.warnings) == 1
    warning = settings.warnings[0]
    assert "Kubernetes service link" in warning
    assert "DL4TV_HTTP_PORT" in warning
    assert "enableServiceLinks" in warning


def test_http_port_wins_over_an_injected_service_link(clean_env):
    clean_env.setenv("DL4TV_PORT", "tcp://10.43.211.198:8484")
    clean_env.setenv("DL4TV_HTTP_PORT", "9000")

    settings = env()

    assert settings.port == 9000
    assert settings.warnings == ()


def test_nonsense_http_port_falls_back_with_a_warning(clean_env):
    clean_env.setenv("DL4TV_HTTP_PORT", "not-a-port")

    settings = env()

    assert settings.port == DEFAULT_PORT
    assert "DL4TV_HTTP_PORT" in settings.warnings[0]


def test_blank_values_are_ignored(clean_env):
    clean_env.setenv("DL4TV_PORT", "   ")
    clean_env.setenv("DL4TV_HTTP_PORT", "")

    assert env().port == DEFAULT_PORT
    assert env().warnings == ()


def test_public_url_trailing_slash_is_trimmed(clean_env):
    clean_env.setenv("DL4TV_PUBLIC_URL", "https://dl4tv.example.com/")

    assert env().public_url == "https://dl4tv.example.com"
