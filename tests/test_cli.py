from __future__ import annotations

from pathlib import Path

import pytest

from ncm_cloud_sync.cli import main


def test_scan_command(tmp_path: Path, capsys) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    code = main(["--music-dir", str(music), "scan"])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 个音频文件" in out


def test_config_command(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("music_dirs: []\ndata_dir: data\n", encoding="utf-8")
    code = main(["-c", str(config), "config"])
    assert code == 0
    out = capsys.readouterr().out
    assert "music_dirs" in out
    assert "cookie" in out


def test_sync_without_login_returns_error(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    code = main(["--music-dir", str(music), "sync"])
    assert code == 2


def test_version_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0

def test_config_dry_run_applies_to_sync_and_daemon(tmp_path: Path, monkeypatch) -> None:
    import argparse
    from types import SimpleNamespace

    from ncm_cloud_sync import cli
    from ncm_cloud_sync.config import Config

    config = Config(music_dirs=[tmp_path], data_dir=tmp_path / "data", dry_run=True)
    captured = {}

    class Dummy:
        def close(self) -> None:
            pass

    class DummySyncer:
        def __init__(self, config, client, state, **kwargs):
            captured.update(kwargs)

        def run_once(self):
            return SimpleNamespace(failed=0, summary=lambda: "ok")

    monkeypatch.setattr(cli, "_client", lambda config: Dummy())
    monkeypatch.setattr(cli, "_state", lambda config: Dummy())
    monkeypatch.setattr(cli, "Syncer", DummySyncer)
    monkeypatch.setattr(cli, "write_last_run", lambda *args: None)

    args = argparse.Namespace(dry_run=False, force=False, limit=None)
    assert cli.cmd_sync(args, config) == 0
    assert captured["dry_run"] is True

    daemon_captured = {}
    monkeypatch.setattr(cli, "run_daemon", lambda *a, **kw: daemon_captured.update(kw))
    daemon_args = argparse.Namespace(dry_run=False, force=False, no_run_now=True)
    assert cli.cmd_daemon(daemon_args, config) == 0
    assert daemon_captured["dry_run"] is True
