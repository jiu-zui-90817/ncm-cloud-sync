from __future__ import annotations

from pathlib import Path

import pytest

from ncm_cloud_sync.config import Config
from ncm_cloud_sync.errors import ApiError, ConfigError
from ncm_cloud_sync.signin import attempt_daily_signin, signin_status
from ncm_cloud_sync.state import StateStore


class FakeSigninClient:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or {
            "already": False,
            "message": "签到成功",
            "points": 2,
        }
        self.error = error
        self.calls = 0

    def daily_signin(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.result)


def make_config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data", daily_signin=True)


def test_daily_signin_succeeds_and_skips_second_attempt(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient()
    with StateStore(config.db_path) as state:
        first = attempt_daily_signin(config, client, state)
        assert first["status"] == "success"
        assert first["points"] == 2
        assert first["today"] is True
        second = attempt_daily_signin(config, client, state)
        assert second["status"] == "success"
        assert second["skipped"] is True
        assert client.calls == 1


def test_daily_signin_failure_is_recorded_and_not_retried_automatically(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient(error=ApiError("风控", code=-462))
    with StateStore(config.db_path) as state:
        first = attempt_daily_signin(config, client, state)
        assert first["status"] == "failed"
        assert "风控" in first["message"]
        second = attempt_daily_signin(config, client, state)
        assert second["status"] == "failed"
        assert second["skipped"] is True
        assert client.calls == 1


def test_daily_signin_manual_can_retry_same_day_failure(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient(error=ApiError("网络错误"))
    with StateStore(config.db_path) as state:
        attempt_daily_signin(config, client, state)
        client.error = None
        retried = attempt_daily_signin(config, client, state, manual=True)
        assert retried["status"] == "success"
        assert retried["skipped"] is False
        assert client.calls == 2


def test_daily_signin_can_be_already_signed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient(
        result={"already": True, "message": "今天已经签到过了", "points": None}
    )
    with StateStore(config.db_path) as state:
        result = attempt_daily_signin(config, client, state)
        assert result["status"] == "already"
        assert result["message"] == "今天已经签到过了"


def test_daily_signin_disabled_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.daily_signin = False
    config.ensure_data_dir()
    with StateStore(config.db_path) as state:
        with pytest.raises(ConfigError, match="启用每日签到"):
            attempt_daily_signin(config, FakeSigninClient(), state)
        assert signin_status(state)["status"] == ""
