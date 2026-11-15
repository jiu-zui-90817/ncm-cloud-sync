"""配置加载：YAML + 环境变量。

优先级：CLI 参数 > 环境变量 > YAML 配置文件 > 内置默认值。
配置文件位置：--config 指定；否则 WY_MUSIC_CONFIG；否则依次尝试
./config.yaml 与 /config/config.yaml（Docker 默认挂载点）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ConfigError

DEFAULT_CONFIG_CANDIDATES = (Path("config.yaml"), Path("/config/config.yaml"))
DEFAULT_AUDIO_EXTENSIONS = (
    "mp3",
    "flac",
    "m4a",
    "aac",
    "ogg",
    "opus",
    "wav",
    "wma",
    "ape",
)
# 网易云云盘对部分格式支持有限，默认只上传常见格式。
DEFAULT_UPLOAD_EXTENSIONS = ("mp3", "flac", "m4a", "aac", "ogg", "opus", "wav")


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace(",", " ").split()
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _normalise_extensions(values: Any) -> tuple[str, ...]:
    result: list[str] = []
    for item in _as_list(values):
        ext = item.lower().lstrip(".")
        if ext and ext not in result:
            result.append(ext)
    return tuple(result)


def _resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


@dataclass
class Config:
    music_dirs: list[Path] = field(default_factory=list)
    data_dir: Path = Path("data")
    cookie: str = ""
    cookie_file: Path | None = None
    extensions: tuple[str, ...] = DEFAULT_AUDIO_EXTENSIONS
    upload_extensions: tuple[str, ...] = DEFAULT_UPLOAD_EXTENSIONS
    interval: int = 3600
    rate_limit: float = 1.0
    timeout: float = 30.0
    retries: int = 3
    min_size: int = 0
    max_upload_size: int = 0
    exclude: tuple[str, ...] = (".*", "@eaDir", "#recycle", "lost+found")
    exclude_dirs: tuple[Path, ...] = ()
    dry_run: bool = False
    log_level: str = "INFO"
    config_path: Path | None = None

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def qr_path(self) -> Path:
        return self.data_dir / "qr.png"

    @property
    def last_run_path(self) -> Path:
        return self.data_dir / "last_run.json"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass

    def redacted(self) -> dict[str, Any]:
        return {
            "music_dirs": [str(path) for path in self.music_dirs],
            "data_dir": str(self.data_dir),
            "cookie": "***" if self.cookie else "",
            "cookie_file": str(self.cookie_file) if self.cookie_file else None,
            "extensions": list(self.extensions),
            "upload_extensions": list(self.upload_extensions),
            "interval": self.interval,
            "rate_limit": self.rate_limit,
            "timeout": self.timeout,
            "retries": self.retries,
            "min_size": self.min_size,
            "max_upload_size": self.max_upload_size,
            "exclude": list(self.exclude),
            "exclude_dirs": [str(path) for path in self.exclude_dirs],
            "dry_run": self.dry_run,
            "log_level": self.log_level,
        }

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        validate: bool = True,
        **overrides: Any,
    ) -> "Config":
        env = env if env is not None else os.environ
        yaml_path = cls._find_config_path(config_path, env)
        raw: dict[str, Any] = {}
        base = Path.cwd()
        if yaml_path is not None:
            raw = cls._read_yaml(yaml_path)
            base = yaml_path.parent

        values: dict[str, Any] = {}
        if "music_dirs" in raw:
            values["music_dirs"] = [
                _resolve_path(item, base) for item in _as_list(raw["music_dirs"])
            ]
        if "data_dir" in raw:
            values["data_dir"] = _resolve_path(raw["data_dir"], base)
        if "cookie" in raw:
            values["cookie"] = str(raw["cookie"] or "")
        if "cookie_file" in raw and raw["cookie_file"]:
            values["cookie_file"] = _resolve_path(raw["cookie_file"], base)
        if "extensions" in raw:
            values["extensions"] = _normalise_extensions(raw["extensions"])
        if "upload_extensions" in raw:
            values["upload_extensions"] = _normalise_extensions(raw["upload_extensions"])
        for key in (
            "interval",
            "rate_limit",
            "timeout",
            "retries",
            "min_size",
            "max_upload_size",
        ):
            if key in raw and raw[key] is not None:
                values[key] = type(getattr(cls, key))(raw[key])
        if "exclude" in raw:
            values["exclude"] = tuple(_as_list(raw["exclude"]))
        if "exclude_dirs" in raw:
            values["exclude_dirs"] = tuple(
                _resolve_path(item, base) for item in _as_list(raw["exclude_dirs"])
            )
        if "dry_run" in raw:
            values["dry_run"] = parse_bool(raw["dry_run"])
        if "log_level" in raw:
            values["log_level"] = str(raw["log_level"]).upper()

        values.update(cls._from_env(env, base))

        for key, value in overrides.items():
            if value is not None:
                values[key] = value

        config = cls(**values)
        config.config_path = yaml_path
        if validate:
            config.validate()
        return config

    @classmethod
    def _find_config_path(
        cls, config_path: str | Path | None, env: Mapping[str, str]
    ) -> Path | None:
        candidate = config_path or env.get("WY_MUSIC_CONFIG")
        if candidate:
            path = Path(candidate).expanduser()
            if not path.is_file():
                raise ConfigError(f"配置文件不存在: {path}")
            return path.resolve()
        for path in DEFAULT_CONFIG_CANDIDATES:
            if path.is_file():
                return path.resolve()
        return None

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
            raise ConfigError("缺少 PyYAML，请先安装依赖: pip install PyYAML") from exc
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except OSError as exc:
            raise ConfigError(f"读取配置文件失败: {exc}") from exc
        except Exception as exc:
            raise ConfigError(f"解析配置文件失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("配置文件顶层必须是对象")
        return data

    @classmethod
    def _from_env(cls, env: Mapping[str, str], base: Path) -> dict[str, Any]:
        values: dict[str, Any] = {}

        def text(name: str) -> str | None:
            value = env.get(name)
            return value.strip() if value and value.strip() else None

        dirs = text("WY_MUSIC_DIRS")
        if dirs:
            values["music_dirs"] = [
                _resolve_path(item, base) for item in _as_list(dirs)
            ]
        data_dir = text("WY_MUSIC_DATA_DIR")
        if data_dir:
            values["data_dir"] = _resolve_path(data_dir, base)
        cookie = text("WY_MUSIC_COOKIE")
        if cookie:
            values["cookie"] = cookie
        cookie_file = text("WY_MUSIC_COOKIE_FILE")
        if cookie_file:
            values["cookie_file"] = _resolve_path(cookie_file, base)
        for name, key in (
            ("WY_MUSIC_EXTENSIONS", "extensions"),
            ("WY_MUSIC_UPLOAD_EXTENSIONS", "upload_extensions"),
        ):
            value = text(name)
            if value:
                values[key] = _normalise_extensions(value)
        exclude = text("WY_MUSIC_EXCLUDE")
        if exclude:
            values["exclude"] = tuple(_as_list(exclude))
        exclude_dirs = text("WY_MUSIC_EXCLUDE_DIRS")
        if exclude_dirs:
            values["exclude_dirs"] = tuple(
                _resolve_path(item, base) for item in _as_list(exclude_dirs)
            )
        for name, key in (
            ("WY_MUSIC_INTERVAL", "interval"),
            ("WY_MUSIC_RATE_LIMIT", "rate_limit"),
            ("WY_MUSIC_TIMEOUT", "timeout"),
            ("WY_MUSIC_RETRIES", "retries"),
            ("WY_MUSIC_MIN_SIZE", "min_size"),
            ("WY_MUSIC_MAX_UPLOAD_SIZE", "max_upload_size"),
        ):
            value = text(name)
            if value:
                try:
                    values[key] = type(getattr(cls, key))(value)
                except ValueError as exc:
                    raise ConfigError(f"{name} 不是合法数字: {value}") from exc
        dry_run = text("WY_MUSIC_DRY_RUN")
        if dry_run is not None:
            values["dry_run"] = parse_bool(dry_run)
        log_level = text("WY_MUSIC_LOG_LEVEL")
        if log_level:
            values["log_level"] = log_level.upper()
        return values

    def validate(self) -> None:
        if not self.music_dirs:
            raise ConfigError(
                "没有配置音乐目录。请设置 WY_MUSIC_DIRS 或在 config.yaml 中配置 music_dirs。"
            )
        for directory in self.music_dirs:
            if not directory.is_dir():
                raise ConfigError(f"音乐目录不存在或不可读: {directory}")
        if not self.extensions:
            raise ConfigError("extensions 不能为空")
        if not self.upload_extensions:
            raise ConfigError("upload_extensions 不能为空")
        if self.interval < 0:
            raise ConfigError("interval 不能为负数")
        if self.rate_limit < 0:
            raise ConfigError("rate_limit 不能为负数")
        if self.retries < 0:
            raise ConfigError("retries 不能为负数")
        for directory in self.exclude_dirs:
            if not directory.is_absolute():
                raise ConfigError(f"排除目录必须是绝对路径: {directory}")

    def with_overrides(self, **kwargs: Any) -> "Config":
        return replace(self, **{key: value for key, value in kwargs.items() if value is not None})
