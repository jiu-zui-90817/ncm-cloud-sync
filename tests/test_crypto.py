from __future__ import annotations

import base64
import hashlib
import json

import pytest
from Crypto.Cipher import AES

from wy_music_cloud.crypto import (
    EAPI_KEY,
    IV,
    PRESET_KEY,
    PUBLIC_KEY_B64,
    anonymous_username,
    encrypt_eapi,
    encrypt_weapi,
    random_secret,
    rsa_encrypt_secret,
)


def unpad(data: bytes) -> bytes:
    return data[:-data[-1]]


def decrypt_params(form: dict, secret: str) -> dict:
    step = AES.new(secret.encode(), AES.MODE_CBC, IV).decrypt(
        base64.b64decode(form["params"])
    )
    inner = base64.b64decode(unpad(step))
    raw = AES.new(PRESET_KEY, AES.MODE_CBC, IV).decrypt(inner)
    return json.loads(unpad(raw))


def test_random_secret_is_base62_and_16_chars() -> None:
    secret = random_secret()
    assert len(secret) == 16
    assert all(ch.isalnum() for ch in secret)
    assert secret != random_secret()


def test_encrypt_weapi_roundtrip() -> None:
    payload = {"limit": 10, "offset": 0, "name": "周杰伦", "nested": {"a": [1, 2]}}
    secret = "AbCdEfGhIjKlMnOp"
    form = encrypt_weapi(payload, secret)
    assert set(form) == {"params", "encSecKey"}
    assert len(form["encSecKey"]) == 256
    assert decrypt_params(form, secret) == payload


def test_encrypt_weapi_is_deterministic_for_fixed_secret() -> None:
    assert encrypt_weapi({"a": 1}, "0" * 16) == encrypt_weapi({"a": 1}, "0" * 16)


def test_encrypt_weapi_rejects_bad_secret() -> None:
    with pytest.raises(ValueError):
        encrypt_weapi({"a": 1}, "short")


def test_public_key_is_1024_bit() -> None:
    assert len(base64.b64decode(PUBLIC_KEY_B64)) > 0
    assert len(rsa_encrypt_secret("0" * 16)) == 256


def test_anonymous_username_matches_known_vector() -> None:
    device_id = "0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123"
    assert anonymous_username(device_id) == (
        "MDEyMzQ1Njc4OUFCQ0RFRjAxMjM0NTY3ODlBQkNERUYwMTIzNDU2Nzg5QUJDREVGMDEyMy"
        "BYa2pIc2o5dnlXcTVRNDdCdXYyVWNnPT0="
    )


def test_encrypt_eapi_roundtrip_and_signature() -> None:
    uri = "/api/login/qrcode/unikey"
    payload = {"type": 3, "header": {"os": "pc", "deviceId": "ABC"}}
    form = encrypt_eapi(uri, payload)
    raw = unpad(AES.new(EAPI_KEY, AES.MODE_ECB).decrypt(bytes.fromhex(form["params"]))).decode()
    prefix = uri + "-36cd479b6b5-"
    assert raw.startswith(prefix)
    text, _, digest = raw[len(prefix):].partition("-36cd479b6b5-")
    assert json.loads(text) == payload
    assert digest == hashlib.md5(f"nobody{uri}use{text}md5forencrypt".encode()).hexdigest()
