from __future__ import annotations

from wy_music_cloud.updates import UpdateChecker, is_newer, parse_version


def test_parse_version_and_newer() -> None:
    assert parse_version("v0.2.21") == (0, 2, 21)
    assert parse_version("release-1.0") == (1, 0)
    assert is_newer("0.2.21", "0.2.20") is True
    assert is_newer("v0.2.20", "0.2.20") is False
    assert is_newer("0.2.9", "0.2.10") is False
    assert is_newer("", "0.2.20") is False


def test_update_checker_caches_until_forced() -> None:
    calls: list[int] = []

    def fake_fetch():
        calls.append(1)
        return {
            "latest": "0.2.21",
            "release_url": "https://example.com/release",
            "published_at": "2026-11-15T00:00:00Z",
            "notes": "notes",
        }

    checker = UpdateChecker("0.2.20", fetch=fake_fetch, ttl=600)
    first = checker.check()
    assert first["update_available"] is True
    assert first["latest"] == "0.2.21"
    assert first["error"] == ""
    assert checker.check() == first
    assert len(calls) == 1

    forced = checker.check(force=True)
    assert forced["latest"] == "0.2.21"
    assert len(calls) == 2


def test_update_checker_returns_error_result() -> None:
    def broken():
        raise RuntimeError("network down")

    checker = UpdateChecker("0.2.20", fetch=broken, ttl=600)
    result = checker.check()
    assert result["update_available"] is False
    assert "检查更新失败" in result["error"]
    assert "network down" in result["error"]
    assert checker.check()["error"] == result["error"]
