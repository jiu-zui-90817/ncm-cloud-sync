from __future__ import annotations

import pytest

from wy_music_cloud.cookies import (
    cookie_header,
    ensure_defaults,
    parse_cookie,
    redact,
)


def test_parse_cookie_header() -> None:
    cookies = parse_cookie("MUSIC_U=abc123; __csrf=xyz; os=pc")
    assert cookies["MUSIC_U"] == "abc123"
    assert cookies["__csrf"] == "xyz"


def test_parse_cookie_json_and_bare_value() -> None:
    assert parse_cookie('{"MUSIC_U": "abc"}') == {"MUSIC_U": "abc"}
    assert parse_cookie("just-a-token") == {"MUSIC_U": "just-a-token"}


def test_parse_cookie_invalid_json_is_config_error() -> None:
    from wy_music_cloud.errors import ConfigError

    with pytest.raises(ConfigError):
        parse_cookie('{"MUSIC_U": ')


def test_ensure_defaults_does_not_override() -> None:
    cookies = ensure_defaults({"MUSIC_U": "x", "os": "linux"})
    assert cookies["os"] == "linux"
    assert cookies["appver"] == "2.7.1.198277"


def test_cookie_header_url_encodes() -> None:
    assert cookie_header({"a": "1 2", "b": "x/y"}) == "a=1%202; b=x%2Fy"


def test_cookie_header_can_keep_raw_values_for_qr() -> None:
    assert cookie_header({"_ntes_nnid": "abc,123"}, quote_values=False) == "_ntes_nnid=abc,123"


def test_redact_hides_secrets() -> None:
    result = redact({"MUSIC_U": "abcdef", "__csrf": "zzz", "os": "pc"})
    assert result["MUSIC_U"] == "abcd***"
    assert result["__csrf"] == "***"
    assert result["os"] == "pc"
