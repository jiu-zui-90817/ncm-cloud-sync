from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ncm_cloud_sync.config import Config
from ncm_cloud_sync.errors import ApiError, ConfigError
from ncm_cloud_sync.signin import (
    SIGNIN_TARGET,
    attempt_daily_signin,
    signin_status,
)
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


def local_now() -> datetime:
    return datetime.now().astimezone()


def make_due(state: StateStore, now: datetime | None = None) -> datetime:
    now = now or local_now()
    # 把今天的随机时刻设成“已经到点”，避免测试依赖当前钟点。
    state.set_meta(SIGNIN_TARGET, now.isoformat(timespec="seconds"))
    return now


def test_daily_signin_succeeds_and_skips_second_attempt(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient()
    with StateStore(config.db_path) as state:
        now = make_due(state)
        first = attempt_daily_signin(config, client, state, now=now)
        assert first["status"] == "success"
        assert first["points"] == 2
        assert first["today"] is True
        second = attempt_daily_signin(config, client, state, now=now)
        assert second["status"] == "success"
        assert second["skipped"] is True
        third = attempt_daily_signin(config, client, state, now=now, manual=True)
        assert third["skipped"] is True
        assert client.calls == 1


def test_daily_signin_failure_is_recorded_and_not_retried_automatically(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient(error=ApiError("风控", code=-462))
    with StateStore(config.db_path) as state:
        now = make_due(state)
        first = attempt_daily_signin(config, client, state, now=now)
        assert first["status"] == "failed"
        assert "风控" in first["message"]
        second = attempt_daily_signin(config, client, state, now=now)
        assert second["status"] == "failed"
        assert second["skipped"] is True
        assert client.calls == 1


def test_daily_signin_manual_can_retry_same_day_failure(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient(error=ApiError("网络错误"))
    with StateStore(config.db_path) as state:
        now = make_due(state)
        attempt_daily_signin(config, client, state, now=now)
        client.error = None
        retried = attempt_daily_signin(config, client, state, manual=True, now=now)
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
        now = make_due(state)
        result = attempt_daily_signin(config, client, state, now=now)
        assert result["status"] == "already"
        assert result["message"] == "今天已经签到过了"


def test_daily_signin_waits_for_random_target(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient()
    now = local_now().replace(hour=9, minute=0, second=0, microsecond=0)
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    with StateStore(config.db_path) as state:
        state.set_meta(SIGNIN_TARGET, target.isoformat(timespec="seconds"))
        early = attempt_daily_signin(config, client, state, now=now)
        assert early["skipped"] is True
        assert early["pending"] is True
        assert client.calls == 0
        later = attempt_daily_signin(config, client, state, now=target)
        assert later["status"] == "success"
        assert client.calls == 1


def test_random_target_is_stable_for_today_and_inside_window(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient()
    now = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    with StateStore(config.db_path) as state:
        first = attempt_daily_signin(config, client, state, now=now)
        second = attempt_daily_signin(config, client, state, now=now)
        assert first["skipped"] is True
        assert first["target"] == second["target"]
        parsed = datetime.fromisoformat(first["target"])
        assert parsed.date() == now.date()
        assert 8 <= parsed.hour < 23
        assert parsed.minute >= 1
        assert client.calls == 0


def test_manual_signin_ignores_random_target(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient()
    now = local_now()
    future = now + timedelta(hours=1)
    with StateStore(config.db_path) as state:
        state.set_meta(SIGNIN_TARGET, future.isoformat(timespec="seconds"))
        result = attempt_daily_signin(config, client, state, manual=True, now=now)
        assert result["status"] == "success"
        assert client.calls == 1


def test_daily_signin_disabled_raises(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.daily_signin = False
    config.ensure_data_dir()
    with StateStore(config.db_path) as state:
        with pytest.raises(ConfigError, match="启用每日签到"):
            attempt_daily_signin(config, FakeSigninClient(), state)
        assert signin_status(state)["status"] == ""
