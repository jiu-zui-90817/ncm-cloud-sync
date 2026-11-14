from __future__ import annotations

import pytest

import wy_music_cloud.login as login
from wy_music_cloud.errors import AuthError


class FakeClient:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.calls = 0

    def qr_unikey(self) -> str:
        return "key"

    def qr_status(self, key: str):
        self.calls += 1
        if self.calls == 1:
            return {"code": 801}
        return {"code": 803, "cookies": "MUSIC_U=abc; __csrf=csrf"}

    def login_status(self):
        return {"nickname": "n", "userId": 1}


def test_qr_login_success(monkeypatch) -> None:
    pytest.importorskip("qrcode")
    monkeypatch.setattr(login.time, "sleep", lambda _: None)
    client = FakeClient()
    cookies, profile = login.qr_login(client, qr_path=None, output=lambda _: None)
    assert cookies["MUSIC_U"] == "abc"
    assert cookies["__csrf"] == "csrf"
    assert profile["userId"] == 1


def test_qr_login_expired(monkeypatch) -> None:
    pytest.importorskip("qrcode")
    monkeypatch.setattr(login.time, "sleep", lambda _: None)

    class Expired(FakeClient):
        def qr_status(self, key: str):
            return {"code": 800}

    with pytest.raises(AuthError):
        login.qr_login(Expired(), qr_path=None, output=lambda _: None)


def test_phone_login(monkeypatch) -> None:
    client = FakeClient()
    client.phone_login = lambda *a, **k: {"code": 200}  # type: ignore[method-assign]
    cookies, profile = login.phone_login(client, "1", "p")
    assert profile["nickname"] == "n"
