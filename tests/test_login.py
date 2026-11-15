from __future__ import annotations

import pytest

import ncm_cloud_sync.login as login
from urllib.parse import parse_qs, urlparse
from ncm_cloud_sync.errors import AuthError


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


def test_build_qr_url_matches_official_desktop_flow() -> None:
    url = login.build_qr_url("abc")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "music.163.com"
    assert parsed.path == "/login"
    assert query["codekey"] == ["abc"]
    assert "chainId" not in query


def test_qr_login_accepts_singular_cookie(monkeypatch) -> None:
    pytest.importorskip("qrcode")
    monkeypatch.setattr(login.time, "sleep", lambda _: None)

    class Singular(FakeClient):
        def qr_status(self, key: str):
            return {"code": 803, "cookie": "MUSIC_U=abc; __csrf=csrf"}

    cookies, _ = login.qr_login(Singular(), qr_path=None, output=lambda _: None)
    assert cookies["MUSIC_U"] == "abc"
    assert cookies["__csrf"] == "csrf"


def test_qr_login_handles_security_verification(monkeypatch) -> None:
    pytest.importorskip("qrcode")
    monkeypatch.setattr(login.time, "sleep", lambda _: None)
    messages: list[str] = []

    class VerifyThenOk(FakeClient):
        def qr_status(self, key: str):
            self.calls += 1
            if self.calls == 1:
                return {"code": 8821}
            return {"code": 803, "cookie": "MUSIC_U=abc"}

    cookies, _ = login.qr_login(
        VerifyThenOk(), qr_path=None, output=messages.append
    )
    assert cookies["MUSIC_U"] == "abc"
    assert any("安全验证" in message for message in messages)


def test_phone_login(monkeypatch) -> None:
    client = FakeClient()
    client.phone_login = lambda *a, **k: {"code": 200}  # type: ignore[method-assign]
    cookies, profile = login.phone_login(client, "1", "p")
    assert profile["nickname"] == "n"
