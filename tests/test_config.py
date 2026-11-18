from __future__ import annotations

from pathlib import Path

import pytest

from ncm_cloud_sync.config import Config, parse_bool
from ncm_cloud_sync.errors import ConfigError


def write_config(tmp_path: Path, music: Path, extra: str = "") -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"music_dirs: [music]\ndata_dir: state\ncookie: MUSIC_U=abc\n{extra}",
        encoding="utf-8",
    )
    return config


def test_load_yaml_resolves_relative_to_config(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    config = write_config(tmp_path, music)
    loaded = Config.load(config, env={})
    assert loaded.music_dirs == [music.resolve()]
    assert loaded.data_dir == (tmp_path / "state").resolve()
    assert loaded.cookie == "MUSIC_U=abc"


def test_env_overrides_yaml(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    config = write_config(tmp_path, music)
    loaded = Config.load(
        config,
        env={
            "NCM_CLOUD_SYNC_DIRS": str(other),
            "NCM_CLOUD_SYNC_INTERVAL": "5",
        },
    )
    assert loaded.music_dirs == [other]
    assert loaded.interval == 5


def test_missing_music_dir_raises(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("music_dirs: [missing]\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(config, env={})


def test_validate_false_allows_empty_dirs() -> None:
    loaded = Config.load(env={}, validate=False)
    assert loaded.music_dirs == []


def test_redacted_hides_cookie(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    config = write_config(tmp_path, music)
    loaded = Config.load(config, env={})
    assert loaded.redacted()["cookie"] == "***"


def test_notify_empty_yaml_and_env(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    config = write_config(tmp_path, music, "notify_empty: true\n")
    assert Config.load(config, env={}).notify_empty is True
    assert Config.load(config, env={"NCM_CLOUD_SYNC_NOTIFY_EMPTY": "false"}).notify_empty is False


def test_post_sync_defaults_and_delete_disables_missing(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    default = Config.load(write_config(tmp_path, music), env={})
    assert default.post_sync_action == "none"
    assert default.move_target is None
    assert default.check_missing is True

    deleting = Config.load(
        write_config(tmp_path, music, "post_sync_action: delete\n"), env={}
    )
    assert deleting.post_sync_action == "delete"
    assert deleting.check_missing is False

    explicit = Config.load(
        write_config(
            tmp_path,
            music,
            "post_sync_action: delete\ncheck_missing: true\n",
        ),
        env={},
    )
    assert explicit.check_missing is True


def test_move_target_must_be_outside_music_dirs(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    inside = music / "archive"
    config = write_config(
        tmp_path,
        music,
        f"post_sync_action: move\nmove_target: {inside}\n",
    )
    with pytest.raises(ConfigError, match="不能位于音乐目录内"):
        Config.load(config, env={})


def test_parse_bool() -> None:
    assert parse_bool("yes") is True
    assert parse_bool("off") is False
    assert parse_bool(None, True) is True
