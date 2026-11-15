"""项目异常类型。"""

from __future__ import annotations


class NcmSyncError(Exception):
    """所有可预期错误的基类。"""


class ConfigError(NcmSyncError):
    """配置缺失或不合法。"""


class AuthError(NcmSyncError):
    """未登录、登录失效或账号不可用。"""


class ApiError(NcmSyncError):
    """网易云接口返回了非成功状态。"""

    def __init__(self, message: str, code: int | str | None = None, url: str = "") -> None:
        self.code = code
        self.url = url
        detail = f"[{code}] {message}" if code is not None else message
        if url:
            detail += f" ({url})"
        super().__init__(detail)


class RateLimitError(ApiError):
    """网易云接口限流（405/429），稍后重试即可。"""


class UploadError(NcmSyncError):
    """上传过程中的网络或协议错误。"""


class NosUnavailableError(UploadError):
    """无法连接网易云 NOS 上传节点，整轮同步应暂停。"""


class StateError(NcmSyncError):
    """本地状态数据库错误。"""
