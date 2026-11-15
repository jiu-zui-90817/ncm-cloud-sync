from __future__ import annotations

from pathlib import Path

import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest
from Crypto.Cipher import AES

import wy_music_cloud.client as client_module
from wy_music_cloud.crypto import EAPI_KEY
from wy_music_cloud.client import CloudClient, NOS_BUCKET, parse_nos_host
from wy_music_cloud.errors import ApiError, AuthError, NosUnavailableError, RateLimitError


def make_client(**kwargs) -> CloudClient:
    return CloudClient({"MUSIC_U": "token", "__csrf": "csrf"}, **kwargs)


def test_check_upload_payload() -> None:
    client = make_client()
    calls: list[tuple] = []

    def fake_weapi(path, payload=None, **kwargs):
        calls.append((path, payload, kwargs))
        return {"needUpload": True, "songId": 12}

    client._weapi = fake_weapi  # type: ignore[method-assign]
    result = client.check_upload("abc", 123, "mp3")
    assert result["need_upload"] is True
    assert result["song_id"] == 12
    path, payload, kwargs = calls[0]
    assert path == "/weapi/cloud/upload/check"
    assert "host" not in kwargs
    assert payload["ext"] == ""
    assert payload["length"] == 123
    assert payload["md5"] == "abc"


def test_check_upload_keeps_zero_song_id() -> None:
    client = make_client()
    client._weapi = lambda *a, **k: {"needUpload": True, "songId": 0}  # type: ignore[method-assign]
    result = client.check_upload("abc", 123, "mp3")
    assert result["song_id"] == 0


def test_alloc_token_sanitizes_name() -> None:
    client = make_client()
    captured = {}

    def fake_weapi(path, payload=None, **kwargs):
        captured.update(payload)
        return {"result": {"resourceId": "rid"}}

    client._weapi = fake_weapi  # type: ignore[method-assign]
    token = client.alloc_token("abc", "a b.c", "mp3", bucket="")
    assert token["resourceId"] == "rid"
    assert captured["filename"] == "ab_c"
    assert captured["bucket"] == ""


def test_phone_login_hashes_password() -> None:
    client = make_client()
    captured: dict = {}

    def fake_weapi(path, payload=None, **kwargs):
        captured.update(payload)
        return {"code": 200}

    client._weapi = fake_weapi  # type: ignore[method-assign]
    client.phone_login("13800000000", "secret")
    import hashlib

    assert captured["password"] == hashlib.md5(b"secret").hexdigest()


def test_client_adds_pc_cookie_defaults() -> None:
    client = CloudClient({})
    assert client.cookies["os"] == "pc"
    assert client.cookies["appver"] == "2.7.1.198277"


def test_finish_upload_matches_official_payload() -> None:
    client = make_client()
    captured: dict = {}

    def fake_weapi(path, payload=None, **kwargs):
        assert path == "/api/upload/cloud/info/v2"
        captured.update(payload)
        return {"code": 200, "songId": "123", "songIdLong": 123}

    client._weapi = fake_weapi  # type: ignore[method-assign]
    new_id = client.finish_upload(
        md5="abc",
        song_id=None,
        filename="A - B.mp3",
        song="B",
        album="未知专辑",
        artist="A",
        resource_id=36656729598,
    )
    assert new_id == 123
    assert captured["resourceId"] == 36656729598
    assert isinstance(captured["resourceId"], int)
    assert "songid" not in captured
    assert captured["filename"] == "A - B.mp3"
    assert captured["song"] == "B"


def test_finish_upload_string_song_id_and_resource_id() -> None:
    client = make_client()
    captured: dict = {}

    def fake_weapi(path, payload=None, **kwargs):
        captured.update(payload)
        return {"code": 200, "songId": "7"}

    client._weapi = fake_weapi  # type: ignore[method-assign]
    new_id = client.finish_upload(
        md5="abc",
        song_id=7,
        filename="A.mp3",
        song="A",
        album="未知专辑",
        artist="A",
        resource_id="42",
    )
    assert new_id == 7
    assert captured["songid"] == "7"
    assert captured["resourceId"] == 42


def test_finish_upload_submits_zero_song_id() -> None:
    client = make_client()
    captured: dict = {}

    def fake_weapi(path, payload=None, **kwargs):
        captured.update(payload or {})
        return {"code": 200, "songId": "5"}

    client._weapi = fake_weapi  # type: ignore[method-assign]
    new_id = client.finish_upload(
        md5="abc",
        song_id=0,
        filename="a.mp3",
        song="a",
        album="未知专辑",
        artist="未知艺术家",
        resource_id=1,
    )
    assert new_id == 5
    assert captured["songid"] == "0"


def test_upload_remote_exists_still_finalizes_metadata(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "whatever.mp3"
    path.write_bytes(b"x")
    client = make_client(rate_limit=0)
    client.check_upload = lambda *a, **k: {"need_upload": False, "song_id": 7}  # type: ignore[method-assign]
    events: list[str] = []
    captured: dict = {}

    def alloc(md5, name, ext, *, bucket=""):
        assert bucket == NOS_BUCKET
        events.append("alloc")
        return {"resourceId": 36656729598, "objectKey": "obj/key", "token": "nos-token"}

    client.alloc_token = alloc  # type: ignore[method-assign]
    client.upload_to_nos = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload bytes"))  # type: ignore[method-assign]
    client.finish_upload = lambda **k: (captured.update(k), 7)[1]  # type: ignore[method-assign]
    client.publish = lambda song_id: events.append(f"pub:{song_id}")  # type: ignore[method-assign]
    monkeypatch.setattr(client_module, "read_tags", lambda p: {"title": "晴天"})

    result = client.upload(path, md5="abc")
    assert result.uploaded is True
    assert result.reason == "uploaded"
    assert result.song_id == 7
    assert captured["song_id"] == 7
    assert captured["resource_id"] == 36656729598
    assert captured["filename"] == "whatever.mp3"
    assert captured["song"] == "晴天"
    assert events == ["alloc", "pub:7"]


def test_upload_full_sequence(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"audio-bytes")
    client = make_client(rate_limit=0)
    events: list[str] = []
    client.check_upload = lambda *a, **k: (events.append("check"), {"need_upload": True, "song_id": 0})[1]  # type: ignore[method-assign]

    def alloc(md5, name, ext, *, bucket=""):
        assert bucket == NOS_BUCKET
        events.append("alloc:" + bucket)
        return {"objectKey": "obj/key", "token": "nos-token", "resourceId": 36656729598}

    client.alloc_token = alloc  # type: ignore[method-assign]
    client.upload_to_nos = lambda *a, **k: events.append("nos")  # type: ignore[method-assign]
    captured: dict = {}
    client.finish_upload = lambda **k: (events.append("info"), captured.update(k), 99)[2]  # type: ignore[method-assign]
    client.publish = lambda song_id: events.append(f"pub:{song_id}")  # type: ignore[method-assign]
    monkeypatch.setattr(client_module, "read_tags", lambda p: {"title": "晴天"})

    result = client.upload(path, md5="abc")
    assert result.uploaded is True
    assert result.song_id == 99
    assert result.cloud_name == "晴天"
    assert events == ["check", "alloc:" + NOS_BUCKET, "nos", "info", "pub:99"]
    assert captured["resource_id"] == 36656729598


def test_upload_dry_run_does_not_alloc(tmp_path: Path) -> None:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"x")
    client = make_client()
    client.check_upload = lambda *a, **k: {"need_upload": True, "song_id": 0}  # type: ignore[method-assign]
    client.alloc_token = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not alloc"))  # type: ignore[method-assign]
    result = client.upload(path, md5="abc", dry_run=True)
    assert result.uploaded is False
    assert result.reason == "dry_run"


def test_post_json_retries_5xx(monkeypatch) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500, json={"code": 500})
        return httpx.Response(200, json={"code": 200, "ok": True})

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=2)
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)
    body = client._weapi("/weapi/test", {})
    assert attempts["n"] == 3
    assert body["ok"] is True


def test_auth_code_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 301, "msg": "需要登录"})

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    with pytest.raises(AuthError):
        client._weapi("/weapi/test", {})


def test_api_error_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 400, "msg": "bad"})

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    with pytest.raises(ApiError):
        client._weapi("/weapi/test", {})


def test_weapi_rewrites_api_path_to_weapi() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"code": 200})

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    client._weapi("/api/cloud/upload/check", {})
    assert seen == ["/weapi/cloud/upload/check"]


def test_cloud_songs_pagination() -> None:
    client = make_client()

    def fake_weapi(path, payload=None, **kwargs):
        if payload["offset"] == 0:
            return {"code": 200, "count": 3, "data": [{"songId": 1}, {"songId": 2}]}
        return {"code": 200, "count": 3, "data": [{"songId": 3}]}

    client._weapi = fake_weapi  # type: ignore[method-assign]
    songs = client.cloud_songs(page_size=2)
    assert [song["songId"] for song in songs] == [1, 2, 3]


def test_parse_nos_host_accepts_top_level_upload_urls() -> None:
    body = {"lbs": "https://wanproxy.127.net/lbs", "upload": ["http://nosup-jd1.127.net"]}
    assert parse_nos_host(body) == "nosup-jd1.127.net"


def test_parse_nos_host_accepts_legacy_data_upload() -> None:
    body = {"data": {"upload": [{"host": "http://nosup-hz1.127.net"}]}}
    assert parse_nos_host(body) == "nosup-hz1.127.net"


def test_get_nos_host_falls_back_to_http_lbs() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.scheme == "https":
            raise httpx.ConnectError("Connection refused", request=request)
        return httpx.Response(
            200,
            json={"lbs": "https://wanproxy.127.net/lbs", "upload": ["http://nosup-jd1.127.net"]},
        )

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    assert client._get_nos_host() == "nosup-jd1.127.net"
    assert seen[0].startswith("https://")
    assert seen[1].startswith("http://")


def test_get_nos_host_uses_direct_host_when_lbs_fails() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method + " " + str(request.url))
        if request.method == "HEAD":
            return httpx.Response(403)
        raise httpx.ConnectError("Connection refused", request=request)

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    assert client._get_nos_host() == "nosup-jd1.127.net"
    assert any(item.startswith("HEAD http://nosup-jd1.127.net/") for item in seen)


def test_get_nos_host_raises_clear_error_when_all_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    with pytest.raises(NosUnavailableError) as exc_info:
        client._get_nos_host()
    assert "无法获取网易云上传节点" in str(exc_info.value)


def test_nos_upload_url_and_headers(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "a.mp3"
    path.write_bytes(b"abc")
    client = make_client()
    monkeypatch.setattr(client, "_get_nos_host", lambda: "upload.example")
    captured = {}

    def fake_post(url, content=None, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return httpx.Response(200)

    monkeypatch.setattr(client._http, "post", fake_post)
    client.upload_to_nos({"objectKey": "a/b", "token": "tok"}, path, "md5", "mp3")
    assert captured["url"].startswith(
        "http://upload.example/" + NOS_BUCKET + "/a%2Fb?"
    )
    assert captured["headers"]["x-nos-token"] == "tok"
    assert captured["headers"]["Content-MD5"] == "md5"


def _decode_eapi(request: httpx.Request) -> tuple[str, dict, str]:
    params = parse_qs(request.content.decode("utf-8"))["params"][0]
    raw = AES.new(EAPI_KEY, AES.MODE_ECB).decrypt(bytes.fromhex(params))
    text = raw[: -raw[-1]].decode("utf-8")
    uri, _, rest = text.partition("-36cd479b6b5-")
    body, _, digest = rest.partition("-36cd479b6b5-")
    return uri, json.loads(body), digest


def test_qr_unikey_uses_eapi_pc_identity() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["cookie"] = request.headers.get("cookie", "")
        seen["uri"], seen["payload"], _digest = _decode_eapi(request)
        return httpx.Response(
            200,
            json={"code": 200, "unikey": "key"},
            headers={"set-cookie": "NMTID=nmtid; Path=/; Domain=.music.163.com"},
        )

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    assert client.qr_unikey() == "key"
    assert seen["url"] == "https://interfacepc.music.163.com/eapi/login/qrcode/unikey"
    assert seen["uri"] == "/api/login/qrcode/unikey"
    assert seen["payload"]["type"] == 3
    assert seen["payload"]["e_r"] is False
    header = seen["payload"]["header"]
    assert header["os"] == "pc"
    assert header["appver"] == client_module.ANON_APPVER
    assert len(header["deviceId"]) == 52
    assert header["deviceId"].isupper()
    assert "deviceId=" in seen["cookie"]
    assert "appver=3.1.17.204416" in seen["cookie"]
    assert client.cookies["NMTID"] == "nmtid"


def test_qr_status_absorbs_last_nonempty_set_cookie() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["uri"], seen["payload"], _digest = _decode_eapi(request)
        return httpx.Response(
            200,
            json={"code": 803},
            headers=[
                ("set-cookie", "MUSIC_U=first; Path=/"),
                ("set-cookie", "MUSIC_U=; Max-Age=0; Path=/"),
                ("set-cookie", "MUSIC_U=final; Path=/"),
                ("set-cookie", "__csrf=csrf2; Path=/"),
            ],
        )

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    assert client.qr_status("key")["code"] == 803
    assert seen["url"] == "https://interfacepc.music.163.com/eapi/login/qrcode/client/login"
    assert seen["uri"] == "/api/login/qrcode/client/login"
    assert seen["payload"]["key"] == "key"
    assert seen["payload"]["type"] == 3
    assert client.cookies["MUSIC_U"] == "final"
    assert client.cookies["__csrf"] == "csrf2"


def test_login_status_requires_profile() -> None:
    client = make_client()
    client._weapi = lambda *a, **k: {"code": 200, "profile": None, "account": None}  # type: ignore[method-assign]
    with pytest.raises(AuthError):
        client.login_status()


def test_absorb_set_cookie() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 200},
            headers={"set-cookie": "MUSIC_U=new; Path=/"},
        )

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    client._weapi("/weapi/test", {})
    assert client.cookies["MUSIC_U"] == "new"


def test_absorb_cookies_handles_same_name_on_different_paths() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 803},
            headers=[
                ("set-cookie", "MUSIC_A_T=old; Path=/eapi/feedback"),
                ("set-cookie", "MUSIC_A_T=new; Path=/api/feedback"),
                ("set-cookie", "MUSIC_U=final; Path=/; Domain=.music.163.com"),
            ],
        )

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0)
    assert client.qr_status("key")["code"] == 803
    assert client.cookies["MUSIC_A_T"] == "new"
    assert client.cookies["MUSIC_U"] == "final"


def test_rate_limit_code_raises_specific_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 405, "message": "操作频繁，请稍候再试"})

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0, rate_limit=0)
    with pytest.raises(RateLimitError) as info:
        client._weapi("/weapi/nos/token/alloc", {})
    assert info.value.code == 405
    assert "操作频繁" in str(info.value)


def test_post_json_throttles_requests(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200})

    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    client = CloudClient(
        {}, transport=httpx.MockTransport(handler), retries=0, rate_limit=0.2
    )
    client._weapi("/weapi/test", {})
    client._weapi("/weapi/test", {})
    assert sleeps, "第二次请求应等待请求间隔"
    assert all(seconds > 0 for seconds in sleeps)
