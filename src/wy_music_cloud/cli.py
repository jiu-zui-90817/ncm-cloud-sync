"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .client import CloudClient
from .config import Config
from .daemon import run_daemon, write_last_run
from .errors import AuthError, ConfigError, WyMusicError
from .login import phone_login, qr_login
from .scanner import scan
from .session import resolve_cookies, save_session
from .state import StateStore
from .syncer import Syncer

logger = logging.getLogger("wy_music_cloud")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wy-music-cloud",
        description="把本地音乐自动同步到网易云音乐云盘（带本地 + 云端去重）。",
    )
    parser.add_argument("-c", "--config", help="配置文件路径（默认 config.yaml 或 /config/config.yaml）")
    parser.add_argument("--log-level", help="日志级别: DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--music-dir", action="append", default=None, help="覆盖音乐目录，可重复")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="登录并保存会话（默认扫码）")
    login.add_argument("--cookie", help="直接粘贴 Cookie 字符串")
    login.add_argument("--cookie-file", help="从文件读取 Cookie")
    login.add_argument("--phone", help="手机号登录")
    login.add_argument("--password", help="手机号密码（不推荐写在命令历史里）")
    login.add_argument("--qr-png", help="把二维码同时保存为 PNG")

    sub.add_parser("logout", help="删除本地 session.json")
    sub.add_parser("check", help="检查登录状态与云盘歌曲数")

    scan_cmd = sub.add_parser("scan", help="只扫描本地文件，不上传")
    scan_cmd.add_argument("--json", action="store_true", help="输出 JSON")

    sync = sub.add_parser("sync", help="执行一次同步")
    sync.add_argument("--dry-run", action="store_true", help="只检查，不真正上传")
    sync.add_argument("--force", action="store_true", help="忽略未变更缓存，重新检查")
    sync.add_argument("--limit", type=int, default=None, help="最多处理 N 个文件，便于试跑")

    daemon = sub.add_parser("daemon", help="常驻轮询同步")
    daemon.add_argument("--dry-run", action="store_true", help="只检查，不真正上传")
    daemon.add_argument("--force", action="store_true", help="忽略未变更缓存")
    daemon.add_argument("--no-run-now", action="store_true", help="启动后先等待一个周期")

    status = sub.add_parser("status", help="查看本地状态与最近运行")
    status.add_argument("--json", action="store_true")

    sub.add_parser("config", help="打印生效配置（隐藏 Cookie）")
    sub.add_parser("reset-failed", help="把 failed 状态重置为 pending，下次重试")
    return parser


def _load_config(args: argparse.Namespace, *, validate: bool = True) -> Config:
    overrides: dict[str, Any] = {}
    if args.music_dir:
        overrides["music_dirs"] = [Path(item).expanduser() for item in args.music_dir]
    if args.log_level:
        overrides["log_level"] = args.log_level.upper()
    return Config.load(args.config, validate=validate, **overrides)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _client(config: Config) -> CloudClient:
    cookies, source = resolve_cookies(config)
    if not cookies:
        raise AuthError(
            "没有可用的登录凭证。请先执行 wy-music-cloud login，"
            "或设置 WY_MUSIC_COOKIE / cookie_file。"
        )
    logger.debug("使用登录凭证来源: %s", source)
    return CloudClient(
        cookies,
        timeout=config.timeout,
        retries=config.retries,
        rate_limit=config.rate_limit,
    )


def _state(config: Config) -> StateStore:
    return StateStore(config.db_path)


def cmd_login(args: argparse.Namespace, config: Config) -> int:
    config.ensure_data_dir()
    cookies: dict[str, str] = {}
    profile: dict[str, Any] = {}
    client: CloudClient | None = None
    try:
        if args.cookie or args.cookie_file:
            from .cookies import parse_cookie

            raw = args.cookie
            if not raw and args.cookie_file:
                try:
                    raw = Path(args.cookie_file).read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise ConfigError(f"读取 cookie_file 失败: {exc}") from exc
            cookies = parse_cookie(raw)
            if not cookies.get("MUSIC_U"):
                raise AuthError("Cookie 中缺少 MUSIC_U")
            client = CloudClient(cookies, timeout=config.timeout, retries=config.retries)
            profile = client.login_status()
        elif args.phone:
            if not args.password:
                raise AuthError("使用 --phone 时必须同时提供 --password")
            client = CloudClient({}, timeout=config.timeout, retries=config.retries)
            cookies, profile = phone_login(client, args.phone, args.password)
        else:
            client = CloudClient({}, timeout=config.timeout, retries=config.retries)
            qr_path = Path(args.qr_png).expanduser() if args.qr_png else config.qr_path
            cookies, profile = qr_login(client, qr_path=qr_path)
        save_session(config.session_path, cookies, profile)
        logger.info("登录信息已保存到 %s", config.session_path)
        return 0
    finally:
        if client is not None:
            client.close()


def cmd_logout(config: Config) -> int:
    try:
        config.session_path.unlink()
        print(f"已删除 {config.session_path}")
    except FileNotFoundError:
        print("没有本地会话文件，无需退出")
    return 0


def cmd_check(config: Config) -> int:
    client = _client(config)
    try:
        profile = client.login_status()
        songs = client.cloud_songs()
        print(f"登录账号: {profile.get('nickname', '')} (userId={profile.get('userId')})")
        print(f"云盘歌曲: {len(songs)} 首")
        return 0
    finally:
        client.close()


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    files = list(scan(config.music_dirs, config.extensions, config.exclude))
    total = sum(item.size for item in files)
    if args.json:
        print(
            json.dumps(
                [{"path": str(item.path), "size": item.size} for item in files],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"扫描到 {len(files)} 个音频文件，共 {total / 1024 / 1024:.1f} MB")
        for item in files[:20]:
            print(f"  {item.path}  {item.size / 1024 / 1024:.1f} MB")
        if len(files) > 20:
            print(f"  ... 其余 {len(files) - 20} 个")
    return 0


def cmd_sync(args: argparse.Namespace, config: Config) -> int:
    client = _client(config)
    state = _state(config)
    try:
        syncer = Syncer(
            config,
            client,
            state,
            dry_run=args.dry_run or config.dry_run,
            force=args.force,
            limit=args.limit,
        )
        stats = syncer.run_once()
        write_last_run(config.last_run_path, stats)
        print(stats.summary())
        return 1 if stats.failed else 0
    finally:
        state.close()
        client.close()


def cmd_daemon(args: argparse.Namespace, config: Config) -> int:
    client = _client(config)
    state = _state(config)
    stop = threading.Event()

    def handle(signum: int, _frame: Any) -> None:
        logger.info("收到信号 %s，准备退出", signum)
        stop.set()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                pass
    try:
        run_daemon(
            config,
            client,
            state,
            stop,
            dry_run=args.dry_run or config.dry_run,
            force=args.force,
            run_immediately=not args.no_run_now,
        )
        return 0
    finally:
        state.close()
        client.close()


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    state = _state(config)
    try:
        stats = state.stats()
        runs = state.recent_runs(5)
        recent = state.recent(10)
        if args.json:
            print(
                json.dumps(
                    {
                        "db": str(config.db_path),
                        "status": stats,
                        "runs": runs,
                        "recent": [
                            {
                                "path": item.path,
                                "status": item.status,
                                "song_id": item.song_id,
                                "last_error": item.last_error,
                            }
                            for item in recent
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(f"状态库: {config.db_path}")
        if not stats:
            print("还没有同步记录")
        else:
            for key in sorted(stats):
                print(f"  {key}: {stats[key]}")
        if runs:
            print("最近运行:")
            for run in runs:
                print(
                    f"  #{run['id']} {run['started_at']} -> {run['finished_at'] or '未完成'} "
                    f"扫描 {run['scanned']} / 上传 {run['uploaded']} / 跳过 {run['skipped']} / 失败 {run['failed']}"
                )
        if recent:
            print("最近文件:")
            for item in recent:
                print(f"  [{item.status}] {item.path}")
        return 0
    finally:
        state.close()


def cmd_config(config: Config) -> int:
    payload = config.redacted()
    payload["config_path"] = str(config.config_path) if config.config_path else None
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_reset_failed(config: Config) -> int:
    state = _state(config)
    try:
        count = state.reset_failed()
        print(f"已重置 {count} 个 failed 文件")
        return 0
    finally:
        state.close()


def dispatch(args: argparse.Namespace) -> int:
    validate = args.command in {"scan", "sync", "daemon"}
    config = _load_config(args, validate=validate)
    _configure_logging(config.log_level)

    if args.command == "login":
        return cmd_login(args, config)
    if args.command == "logout":
        return cmd_logout(config)
    if args.command == "check":
        return cmd_check(config)
    if args.command == "scan":
        return cmd_scan(args, config)
    if args.command == "sync":
        return cmd_sync(args, config)
    if args.command == "daemon":
        return cmd_daemon(args, config)
    if args.command == "status":
        return cmd_status(args, config)
    if args.command == "config":
        return cmd_config(config)
    if args.command == "reset-failed":
        return cmd_reset_failed(config)
    raise ConfigError(f"未知命令: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except WyMusicError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
