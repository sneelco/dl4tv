from __future__ import annotations

import json

import yaml

from app.models import Mapping, VideoRecord
from app.store import Store


def test_creates_default_config_on_first_read(env):
    store = Store(env)
    config = store.config

    assert env.config_file.exists()
    assert config.mappings == []
    assert config.schedule.mode == "daily"
    # The file on disk is valid YAML with a comment header.
    text = env.config_file.read_text()
    assert text.startswith("# dl4tv configuration.")
    assert yaml.safe_load(text)["schedule"]["daily_at"] == "03:00"


def test_config_roundtrips_through_disk(env):
    store = Store(env)
    store.update_config(
        lambda config: config.mappings.append(
            Mapping(playlist_id="PL1", title="Cooking", folder="cooking")
        )
    )

    reloaded = Store(env).config
    assert [m.playlist_id for m in reloaded.mappings] == ["PL1"]
    assert reloaded.mappings[0].folder == "cooking"


def test_invalid_config_is_backed_up_not_fatal(env):
    env.config_file.write_text("schedule: [this is not a schedule]\n")

    config = Store(env).config

    assert config.mappings == []
    assert env.config_file.with_suffix(".yaml.invalid").exists()


def test_state_roundtrips_and_survives_corruption(env):
    store = Store(env)
    store.update_state(
        lambda state: state.playlist("m1").videos.__setitem__(
            "vid1", VideoRecord(video_id="vid1", title="One", status="downloaded")
        )
    )
    assert json.loads(env.state_file.read_text())["playlists"]["m1"]["videos"]["vid1"]["title"] == "One"

    reloaded = Store(env).state
    assert reloaded.playlist("m1").videos["vid1"].status == "downloaded"

    env.state_file.write_text("{not json")
    recovered = Store(env).state
    assert recovered.playlists == {}
    assert env.state_file.with_suffix(".json.invalid").exists()


def test_resolve_folder(env):
    store = Store(env)
    assert store.resolve_folder("cooking") == env.download_dir / "cooking"
    assert store.resolve_folder("/mnt/media/cooking").as_posix() == "/mnt/media/cooking"


def test_token_write_and_clear(env):
    store = Store(env)
    store.write_token({"refresh_token": "abc"})

    assert store.read_token() == {"refresh_token": "abc"}
    store.clear_token()
    assert store.read_token() is None


def test_counts(env):
    store = Store(env)

    def mutate(state):
        playlist = state.playlist("m1")
        playlist.videos["a"] = VideoRecord(video_id="a", status="downloaded")
        playlist.videos["b"] = VideoRecord(video_id="b", status="failed", permanent=True)
        playlist.videos["c"] = VideoRecord(video_id="c", status="skipped")

    store.update_state(mutate)
    counts = store.state.playlist("m1").counts()
    assert counts == {"downloaded": 1, "failed": 1, "skipped": 1, "permanent": 1}
