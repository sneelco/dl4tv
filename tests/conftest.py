from __future__ import annotations

import pytest

from app import settings as settings_module
from app import store as store_module
from app import sync as sync_module


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the app at throwaway config/download directories."""
    config_dir = tmp_path / "config"
    download_dir = tmp_path / "downloads"
    config_dir.mkdir()
    download_dir.mkdir()
    monkeypatch.setenv("DL4TV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("DL4TV_DOWNLOAD_DIR", str(download_dir))
    monkeypatch.delenv("DL4TV_USERNAME", raising=False)
    monkeypatch.delenv("DL4TV_PASSWORD", raising=False)
    settings_module.env.cache_clear()
    store_module.set_store(None)
    sync_module.set_manager(None)
    yield settings_module.env()
    settings_module.env.cache_clear()
    store_module.set_store(None)
    sync_module.set_manager(None)


@pytest.fixture
def store(env):
    return store_module.get_store()
