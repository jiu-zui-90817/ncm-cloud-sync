from __future__ import annotations

import json
from pathlib import Path

import pytest

from wy_music_cloud.errors import ConfigError
from wy_music_cloud.session import load_session, resolve_cookies, save_session


class FakeConfig:
    def __init__(self, tmp_path: Path) -> None:
        self.cookie = ""
        self.cookie_file = None
        self.session_path = tmp_path / "session.json"


def test_save_and_load_session(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    save_session(path, {"MUSIC_U": "abc", "__csrf": "c"}, {"nickname": "n"})
    cookies, profile = load_session(path)
    assert cookies == {"MUSIC_U": "abc", "__csrf": "c"}
    assert profile["nickname"] == "n"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_load_corrupt_session_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("not json", encoding="utf-8")
    assert load_session(path) == ({}, {})


def test_resolve_precedence(tmp_path: Path) -> None:
    config = FakeConfig(tmp_path)
    assert resolve_cookies(config) == ({}, "none")

    save_session(config.session_path, {"MUSIC_U": "session"})
    cookies, source = resolve_cookies(config)
    assert cookies["MUSIC_U"] == "session"
    assert source == "session"

    cookie_file = tmp_path / "cookie.txt"
    cookie_file.write_text("MUSIC_U=file", encoding="utf-8")
    config.cookie_file = cookie_file
    cookies, source = resolve_cookies(config)
    assert cookies["MUSIC_U"] == "file"
    assert source == "cookie_file"

    config.cookie = "MUSIC_U=direct"
    cookies, source = resolve_cookies(config)
    assert cookies["MUSIC_U"] == "direct"
    assert source == "config"


def test_resolve_missing_cookie_file_is_config_error(tmp_path: Path) -> None:
    config = FakeConfig(tmp_path)
    config.cookie_file = tmp_path / "missing.txt"
    with pytest.raises(ConfigError):
        resolve_cookies(config)
