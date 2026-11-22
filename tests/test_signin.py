from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ncm_cloud_sync.config import Config
from ncm_cloud_sync.errors import ApiError, ConfigError
from ncm_cloud_sync.signin import (
    SIGNIN_TARGET,
    SIGNIN_TARGET_WINDOW,
    attempt_daily_signin,
    signin_status,
)
from ncm_cloud_sync.state import StateStore


class FakeSigninClient:
    def __init__(
        self,
        result=None,
        error: Exception | None = None,
    ) -> None:
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


def window_key(start: str = "08:00", end: str = "23:00") -> str:
    return f"{start}-{end}"


def make_due(state: StateStore, now: datetime | None = None) -> datetime:
    now = now or local_now()
    # 把今天的随机时刻设成“已经到点”，避免测试依赖当前钟点。
    state.set_meta(SIGNIN_TARGET, now.isoformat(timespec="seconds"))
    state.set_meta(SIGNIN_TARGET_WINDOW, window_key())
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
        assert "今天已经签到过了" in result["message"]


def test_daily_signin_waits_for_random_target(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient()
    now = local_now().replace(hour=9, minute=0, second=0, microsecond=0)
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    with StateStore(config.db_path) as state:
        state.set_meta(SIGNIN_TARGET, target.isoformat(timespec="seconds"))
        state.set_meta(SIGNIN_TARGET_WINDOW, window_key())
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
        assert client.calls == 0


def test_random_target_respects_custom_window(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.signin_window_start = "10:30"
    config.signin_window_end = "10:35"
    config.ensure_data_dir()
    client = FakeSigninClient()
    now = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    with StateStore(config.db_path) as state:
        first = attempt_daily_signin(config, client, state, now=now)
        assert first["skipped"] is True
        assert first["pending"] is True
        assert first["window_start"] == "10:30"
        assert first["window_end"] == "10:35"
        parsed = datetime.fromisoformat(first["target"])
        assert "10:30" <= parsed.strftime("%H:%M") < "10:35"
        status = signin_status(
            state,
            enabled=True,
            window_start="10:30",
            window_end="10:35",
            now=now,
        )
        assert status["target"] == first["target"]
        assert client.calls == 0


def test_window_change_redraws_same_day_target(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient()
    now = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    with StateStore(config.db_path) as state:
        attempt_daily_signin(config, client, state, now=now)
        config.signin_window_start = "10:00"
        config.signin_window_end = "11:00"
        changed = attempt_daily_signin(config, client, state, now=now)
        assert changed["window_start"] == "10:00"
        assert changed["window_end"] == "11:00"
        parsed = datetime.fromisoformat(changed["target"])
        assert "10:00" <= parsed.strftime("%H:%M") < "11:00"
        assert state.get_meta(SIGNIN_TARGET_WINDOW) == "10:00-11:00"
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


def test_signin_records_cloud_shell_detail(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.ensure_data_dir()
    client = FakeSigninClient(
        result={"already": False, "message": "签到成功，获得 50 云贝", "points": 50}
    )
    with StateStore(config.db_path) as state:
        result = attempt_daily_signin(config, client, state, now=make_due(state))
        assert result["status"] == "success"
        assert result["points"] == 50
        assert client.calls == 1
        assert [item["label"] for item in result["details"]] == ["云贝中心"]
        assert [item["status"] for item in result["details"]] == ["success"]
        assert "50 云贝" in result["message"]
