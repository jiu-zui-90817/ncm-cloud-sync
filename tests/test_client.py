from __future__ import annotations

from pathlib import Path
from typing import Any

import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest
from Crypto.Cipher import AES

import ncm_cloud_sync.client as client_module
from ncm_cloud_sync.crypto import EAPI_KEY
from ncm_cloud_sync.client import CloudClient, NOS_BUCKET, parse_nos_host
from ncm_cloud_sync.errors import ApiError, AuthError, NosUnavailableError, RateLimitError


def make_client(**kwargs) -> CloudClient:
    return CloudClient({"MUSIC_U": "token", "__csrf": "csrf"}, **kwargs)


def test_check_upload_payload() -> None:
    client = make_client()
    calls: list[tuple] = []

    def fake_eapi(path, payload=None, **kwargs):
        calls.append((path, payload, kwargs))
        return {"needUpload": True, "songId": 12}

    client._eapi_api = fake_eapi  # type: ignore[method-assign]
    result = client.check_upload("abc", 123, "mp3")
    assert result["need_upload"] is True
    assert result["song_id"] == 12
    assert result["song_id_raw"] == 12
    path, payload, kwargs = calls[0]
    assert path == "/api/cloud/upload/check"
    assert "host" not in kwargs
    assert payload["ext"] == ""
    assert payload["length"] == 123
    assert payload["md5"] == "abc"


def test_cloud_capacity_reads_top_level_fields() -> None:
    client = make_client()
    calls: list[tuple] = []

    def fake_weapi(path, payload=None, **kwargs):
        calls.append((path, payload, kwargs))
        return {
            "code": 200,
            "maxSize": "1073741824",
            "size": "268435456",
            "count": 12,
            "data": [{"songId": 1}],
        }

    client._weapi = fake_weapi  # type: ignore[method-assign]
    capacity = client.cloud_capacity()

    assert calls[0][0] == "/api/v1/cloud/get"
    assert calls[0][1] == {"limit": 1, "offset": 0}
    assert capacity["supported"] is True
    assert capacity["total_bytes"] == 1073741824
    assert capacity["used_bytes"] == 268435456
    assert capacity["free_bytes"] == 805306368
    assert capacity["percent"] == 25.0
    assert capacity["songs"] == 12
    assert client.last_cloud_capacity == capacity


def test_cloud_capacity_unknown_when_fields_missing() -> None:
    client = make_client()
    client._weapi = lambda *a, **k: {"code": 200, "data": []}  # type: ignore[method-assign]
    capacity = client.cloud_capacity()
    assert capacity["supported"] is False
    assert capacity["total_bytes"] == 0
    assert capacity["percent"] is None


def test_check_upload_keeps_zero_song_id() -> None:
    client = make_client()
    client._eapi_api = lambda *a, **k: {"needUpload": True, "songId": 0}  # type: ignore[method-assign]
    result = client.check_upload("abc", 123, "mp3")
    assert result["song_id"] == 0
    assert result["song_id_raw"] == 0


def test_check_upload_bootstraps_nmtid_for_cloud_info() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(
                200,
                json={"code": 200, "needUpload": True, "songId": 0},
                headers={"set-cookie": "NMTID=from-check; Path=/; Domain=.music.163.com"},
            )
        return httpx.Response(200, json={"code": 200, "songId": "9"})

    client = CloudClient(
        {"MUSIC_U": "music-u", "__csrf": "csrf"},
        transport=httpx.MockTransport(handler),
        retries=0,
        rate_limit=0,
    )
    client.check_upload("abc", 123, "mp3")
    assert client.cookies["NMTID"] == "from-check"
    client._eapi_api("/api/upload/cloud/info/v2", {"md5": "abc", "resourceId": 42})
    assert "NMTID=from-check" in captured[1].headers.get("cookie", "")


def test_whale_alloc_payload() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["cookie"] = request.headers.get("cookie", "")
        captured["method"] = request.method
        captured["form"] = parse_qs(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "objectKey": "obj/key",
                    "token": "nos-token",
                    "resourceId": "84294414064",
                },
            },
        )

    client = CloudClient(
        {"MUSIC_U": "music-u", "__csrf": "csrf"},
        transport=httpx.MockTransport(handler),
        retries=0,
        rate_limit=0,
    )
    token = client.whale_alloc("abc", "A - B.mp3")
    assert token["resourceId"] == "84294414064"
    assert captured["url"] == "http://musicupload.netease.com/api/whale/token/alloc"
    assert captured["method"] == "POST"
    assert "MUSIC_U=music-u" in captured["cookie"]
    assert captured["form"]["channel"] == ["3"]
    assert captured["form"]["filename"] == ["A - B.mp3"]
    assert captured["form"]["md5"] == ["abc"]
    assert captured["form"]["type"] == ["audio"]
    assert captured["form"]["bucket"] == [NOS_BUCKET]
    assert captured["form"]["bizKey"][0]


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

    def fake_eapi(path, payload=None, **kwargs):
        assert path == "/api/upload/cloud/info/v2"
        assert kwargs["base_url"] == client_module.EAPI_UPLOAD_URL
        captured.update(payload)
        return {"code": 200, "songId": "123", "songIdLong": 123}

    client._eapi_native = fake_eapi  # type: ignore[method-assign]
    new_id = client.finish_upload(
        md5="abc",
        song_id=None,
        filename="A - B.mp3",
        song="B",
        album="未知专辑",
        artist="A",
        resource_id=36656729598,
        object_key="jd-musicrep-privatecloud-audio-public/obj/key",
    )
    assert new_id == 123
    assert captured["resourceId"] == 36656729598
    assert captured["songid"] == 0
    assert captured["bitrate"] == 999000
    assert captured["objectKey"] == "jd-musicrep-privatecloud-audio-public/obj/key"
    assert captured["filename"] == "A - B.mp3"
    assert captured["song"] == "B"


def test_finish_upload_string_song_id_and_resource_id() -> None:
    client = make_client()
    captured: dict = {}

    def fake_eapi(path, payload=None, **kwargs):
        captured.update(payload)
        return {"code": 200, "songId": "7"}

    client._eapi_native = fake_eapi  # type: ignore[method-assign]
    new_id = client.finish_upload(
        md5="abc",
        song_id="7",
        filename="A.mp3",
        song="A",
        album="未知专辑",
        artist="A",
        resource_id="42",
        object_key="jd-musicrep-privatecloud-audio-public/obj/key",
    )
    assert new_id == 7
    assert captured["songid"] == "7"
    assert captured["resourceId"] == 42


def test_finish_upload_submits_zero_song_id() -> None:
    client = make_client()
    captured: dict = {}

    def fake_eapi(path, payload=None, **kwargs):
        captured.update(payload or {})
        return {"code": 200, "songId": "5"}

    client._eapi_native = fake_eapi  # type: ignore[method-assign]
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
    assert captured["songid"] == 0
    assert "resourceId" not in captured


def test_upload_remote_exists_still_finalizes_metadata(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "whatever.mp3"
    path.write_bytes(b"x")
    client = make_client(rate_limit=0)
    client.check_upload = lambda *a, **k: {"need_upload": False, "song_id": 7}  # type: ignore[method-assign]
    events: list[str] = []
    captured: dict = {}

    client.whale_alloc = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not alloc"))  # type: ignore[method-assign]
    client.upload_to_nos = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload bytes"))  # type: ignore[method-assign]
    client.finish_upload = lambda **k: (captured.update(k), 7)[1]  # type: ignore[method-assign]
    client.publish = lambda song_id: events.append(f"pub:{song_id}")  # type: ignore[method-assign]
    monkeypatch.setattr(client_module, "read_tags", lambda p: {"title": "晴天"})

    result = client.upload(path, md5="abc")
    assert result.uploaded is True
    assert result.reason == "linked"
    assert result.song_id == 7
    assert captured["song_id"] == 7
    assert captured["resource_id"] == 0
    assert captured["object_key"] is None
    assert captured["filename"] == "whatever.mp3"
    assert captured["song"] == "晴天"
    assert captured["album"] == ""
    assert captured["artist"] == ""
    assert events == ["pub:7"]


def test_cloud_song_md5_map_reads_private_cloud() -> None:
    client = make_client()
    client._weapi = lambda *a, **k: {  # type: ignore[method-assign]
        "count": 2,
        "data": [
            {"privateCloud": {"md5": "AAA", "songId": 12}},
            {"md5": "bbb", "songId": 34},
        ],
    }
    assert client.cloud_song_md5_map() == {"aaa": 12, "bbb": 34}
    assert client.cloud_md5_set() == {"aaa", "bbb"}


def test_cloud_song_index_keeps_metadata() -> None:
    client = make_client()
    client._weapi = lambda *a, **k: {  # type: ignore[method-assign]
        "count": 2,
        "data": [
            {
                "privateCloud": {
                    "md5": "AAA",
                    "songId": 12,
                    "songName": "晴天",
                    "artist": [{"name": "周杰伦"}],
                    "album": {"name": "叶惠美"},
                    "fileSize": 123,
                }
            },
            {"md5": "bbb", "songId": 34, "name": "简单爱", "size": 45},
        ],
    }
    index = client.cloud_song_index()
    assert client.last_cloud_song_count == 2
    assert index["aaa"]["song_id"] == 12
    assert index["aaa"]["name"] == "晴天"
    assert index["aaa"]["artist"] == "周杰伦"
    assert index["aaa"]["album"] == "叶惠美"
    assert index["aaa"]["size"] == 123
    assert index["bbb"]["song_id"] == 34
    assert index["bbb"]["name"] == "简单爱"


def test_upload_full_sequence(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"audio-bytes")
    client = make_client(rate_limit=0)
    events: list[str] = []
    client.check_upload = lambda *a, **k: (events.append("check"), {"need_upload": True, "song_id": 0})[1]  # type: ignore[method-assign]

    def whale(md5, filename):
        assert filename == "song.mp3"
        events.append("whale")
        return {"objectKey": "obj/key", "token": "nos-token", "resourceId": 36656729598}

    client.whale_alloc = whale  # type: ignore[method-assign]
    client.upload_to_nos = lambda *a, **k: events.append("nos")  # type: ignore[method-assign]
    captured: dict = {}
    client.finish_upload = lambda **k: (events.append("info"), captured.update(k), 99)[2]  # type: ignore[method-assign]
    client.publish = lambda song_id: events.append(f"pub:{song_id}")  # type: ignore[method-assign]
    monkeypatch.setattr(client_module, "read_tags", lambda p: {"title": "晴天"})

    result = client.upload(path, md5="abc")
    assert result.uploaded is True
    assert result.song_id == 99
    assert result.cloud_name == "晴天"
    assert events == ["check", "whale", "nos", "info", "pub:99"]
    assert captured["resource_id"] == 36656729598
    assert captured["object_key"] == f"{NOS_BUCKET}/obj/key"


def test_upload_does_not_fall_back_to_old_token_when_whale_fails(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"x")
    client = make_client(rate_limit=0)
    client.check_upload = lambda *a, **k: {"need_upload": True, "song_id": 0}  # type: ignore[method-assign]
    client.whale_alloc = lambda *a, **k: (_ for _ in ()).throw(client_module.UploadError("no whale"))  # type: ignore[method-assign]
    client.alloc_token = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not use old token"))  # type: ignore[method-assign]
    client.upload_to_nos = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not upload"))  # type: ignore[method-assign]
    monkeypatch.setattr(client_module, "read_tags", lambda p: {"title": "晴天"})

    with pytest.raises(client_module.UploadError):
        client.upload(path, md5="abc")


def test_preflight_checks_login_and_upload_check(monkeypatch) -> None:
    client = make_client()
    monkeypatch.setattr(client, "login_status", lambda: {"userId": 7})
    calls = []

    def check_upload(md5, size, ext):
        calls.append((md5, size, ext))
        return {"need_upload": False}

    monkeypatch.setattr(client, "check_upload", check_upload)
    result = client.preflight({"abc": {"size": 123}})
    assert result["user_id"] == 7
    assert result["probe_md5"] == "abc"
    assert result["need_upload"] is False
    assert calls == [("abc", 123, "mp3")]

    # cloud_song_md5_map() 返回 md5 -> songId(int)，也必须能预检。
    result = client.preflight({"def": 1954223039})
    assert result["probe_md5"] == "def"
    assert calls[-1] == ("def", 1024, "mp3")


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


def test_eapi_cloud_info_request_shape() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["ua"] = request.headers.get("user-agent", "")
        captured["cookie"] = request.headers.get("cookie", "")
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 200, "songId": "1"})

    client = CloudClient(
        {"MUSIC_U": "music-u", "__csrf": "csrf", "NMTID": "nmtid"},
        transport=httpx.MockTransport(handler),
        retries=0,
        rate_limit=0,
    )
    body = client._eapi_native(
        "/api/upload/cloud/info/v2",
        {"md5": "abc", "resourceId": 42},
        base_url=client_module.EAPI_UPLOAD_URL,
    )
    assert body["code"] == 200
    assert captured["url"] == "http://musicupload.netease.com/eapi/upload/cloud/info/v2"
    assert "NeteaseMusicDesktop/3.1.42.205594" in captured["ua"]
    assert "MUSIC_U=music-u" in captured["cookie"]
    assert "NMTID=nmtid" in captured["cookie"]

    form = parse_qs(captured["body"])
    plain = AES.new(EAPI_KEY, AES.MODE_ECB).decrypt(bytes.fromhex(form["params"][0]))
    text = plain[:-plain[-1]].decode("utf-8")
    parts = text.split("-36cd479b6b5-")
    assert parts[0] == "/api/upload/cloud/info/v2"
    payload = json.loads(parts[1])
    assert "e_r" not in payload
    assert set(payload["header"]) == {"os", "appver", "deviceId", "requestId", "clientSign"}
    assert payload["header"]["appver"] == "3.1.42.205594"
    assert len(payload["header"]["requestId"]) == 8
    assert "@@@" in payload["header"]["clientSign"]
    assert payload["resourceId"] == 42


def test_publish_uses_eapi() -> None:
    client = make_client()
    captured = {}

    def fake_eapi(path, payload=None, **kwargs):
        captured["path"] = path
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {"code": 200}

    client._eapi_api = fake_eapi  # type: ignore[method-assign]
    client.publish(99)
    assert captured["path"] == "/api/cloud/pub/v2"
    assert captured["payload"] == {"songid": "99"}
    assert captured["kwargs"]["base_url"] == client_module.EAPI_PUB_URL
    assert captured["kwargs"]["check"] is False


def test_publish_accepts_duplicate_code() -> None:
    client = make_client()
    client._eapi_api = lambda *a, **k: {"code": 201}  # type: ignore[method-assign]
    client.publish(1)


def test_publish_raises_on_real_error() -> None:
    client = make_client()
    client._eapi_api = lambda *a, **k: {"code": 400, "message": "bad"}  # type: ignore[method-assign]
    with pytest.raises(ApiError):
        client.publish(1)


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


def test_whale_alloc_maps_rate_limit_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 405, "message": "操作频繁"})

    client = CloudClient({}, transport=httpx.MockTransport(handler), retries=0, rate_limit=0)
    with pytest.raises(RateLimitError):
        client.whale_alloc("abc", "a.mp3")


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


def test_daily_signin_uses_cloud_shell_endpoint(monkeypatch: Any) -> None:
    client = make_client()
    calls: list[tuple[str, dict[str, Any] | None, bool]] = []

    def fake_weapi(path: str, payload=None, *, check=True):
        calls.append((path, payload, check))
        if path.endswith("/point/today/get"):
            return {"code": 200, "data": {"shells": 50}}
        return {"code": 200, "data": {"sign": True}}

    monkeypatch.setattr(client, "_weapi", fake_weapi)
    result = client.daily_signin()
    assert result["already"] is False
    assert result["points"] == 50
    assert "50" in result["message"]
    assert calls == [
        ("/weapi/pointmall/user/sign", {}, False),
        ("/weapi/point/today/get", {}, False),
    ]


def test_daily_signin_already_signed(monkeypatch: Any) -> None:
    client = make_client()
    monkeypatch.setattr(
        client,
        "_weapi",
        lambda *args, **kwargs: {
            "code": 200,
            "data": {"sign": False},
            "message": "今天已经签到过了",
        },
    )
    result = client.daily_signin()
    assert result["already"] is True
    assert result["points"] is None


def test_daily_signin_rejects_legacy_minus_one(monkeypatch: Any) -> None:
    client = make_client()
    monkeypatch.setattr(
        client,
        "_weapi",
        lambda *args, **kwargs: {"code": -1, "message": "功能暂不支持"},
    )
    with pytest.raises(ApiError, match="功能暂不支持"):
        client.daily_signin()


def test_daily_signin_maps_auth_code(monkeypatch: Any) -> None:
    client = make_client()
    monkeypatch.setattr(client, "_weapi", lambda *args, **kwargs: {"code": 301})
    with pytest.raises(AuthError, match="登录"):
        client.daily_signin()


def test_search_songs_parses_cloudsearch_result() -> None:
    client = make_client()
    calls: list[tuple] = []

    def fake_weapi(path, payload=None, **kwargs):
        calls.append((path, payload, kwargs))
        return {
            "code": 200,
            "result": {
                "songs": [
                    {
                        "id": 186016,
                        "name": "晴天",
                        "ar": [{"name": "周杰伦"}],
                        "al": {"name": "叶惠美"},
                    }
                ]
            },
        }

    client._weapi = fake_weapi  # type: ignore[method-assign]
    songs = client.search_songs("晴天 周杰伦", limit=5)

    assert calls[0][0] == "/weapi/cloudsearch/pc"
    assert calls[0][1] == {
        "s": "晴天 周杰伦",
        "type": 1,
        "limit": 5,
        "offset": 0,
        "total": True,
    }
    assert calls[0][2] == {"check": False}
    assert songs == [
        {"id": 186016, "title": "晴天", "artist": "周杰伦", "album": "叶惠美"}
    ]


def test_search_songs_rejects_error_code_and_empty_query() -> None:
    client = make_client()
    calls: list[tuple] = []

    def fake_weapi(path, payload=None, **kwargs):
        calls.append((path, payload, kwargs))
        return {"code": 400, "msg": "参数错误"}

    client._weapi = fake_weapi  # type: ignore[method-assign]
    with pytest.raises(ApiError, match="参数错误"):
        client.search_songs("晴天")
    assert client.search_songs("   ") == []
    assert len(calls) == 1
