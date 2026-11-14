"""网易云 weapi 加密实现。

算法来自 NeteaseCloudMusicApi 的 util/crypto.js：
  1. 明文 JSON 用固定 key 做一次 AES-128-CBC，再 base64；
  2. 结果用随机 16 字节 key 再做一次 AES-128-CBC，再 base64，得到 params；
  3. 随机 key 倒序、左侧补零到 128 字节，用网易云 RSA 公钥做 NO_PADDING
     加密，得到 encSecKey（256 位十六进制）。
"""

from __future__ import annotations

import base64
import json
import os
from functools import lru_cache

from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Util.number import bytes_to_long

IV = b"0102030405060708"
PRESET_KEY = b"0CoJUm6Qyw8W8jud"
BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# 网易云音乐 Web 端固定公钥（1024 bit，e=65537）。
PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/"
    "aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQ"
    "XE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45w"
    "IDAQAB"
)


def random_secret() -> str:
    """生成 16 位 base62 随机密钥。"""
    return "".join(BASE62[b % len(BASE62)] for b in os.urandom(16))


def _pkcs7_pad(data: bytes) -> bytes:
    pad = AES.block_size - len(data) % AES.block_size
    return data + bytes([pad]) * pad


def _aes_encrypt(data: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, IV).encrypt(_pkcs7_pad(data))


@lru_cache(maxsize=1)
def _public_key() -> RSA.RsaKey:
    return RSA.import_key(base64.b64decode(PUBLIC_KEY_B64))


def rsa_encrypt_secret(secret: str) -> str:
    """按网易云的方式做 RSA NO_PADDING 加密，返回 256 个十六进制字符。"""
    key = _public_key()
    size = (key.size_in_bits() + 7) // 8
    block = secret.encode("utf-8")[::-1].rjust(size, b"\x00")
    encrypted = pow(bytes_to_long(block), key.e, key.n)
    return f"{encrypted:0{size * 2}x}"


def encrypt_weapi(payload: dict, secret: str | None = None) -> dict[str, str]:
    """把业务参数加密成 weapi 请求体。"""
    secret = secret or random_secret()
    if len(secret) != 16:
        raise ValueError("weapi secret 必须是 16 个字符")
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    first = _aes_encrypt(raw, PRESET_KEY)
    second = _aes_encrypt(base64.b64encode(first), secret.encode("utf-8"))
    return {
        "params": base64.b64encode(second).decode("ascii"),
        "encSecKey": rsa_encrypt_secret(secret),
    }
