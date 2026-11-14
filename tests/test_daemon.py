from __future__ import annotations

import json
import threading
from pathlib import Path

import wy_music_cloud.daemon as daemon
from wy_music_cloud.config import Config
from wy_music_cloud.syncer import SyncStats


def make_config(tmp_path: Path) -> Config:
    music = tmp_path / "music"
    music.mkdir()
    return Config(
        music_dirs=[music],
        data_dir=tmp_path / "data",
        extensions=("mp3",),
        upload_extensions=("mp3",),
        interval=1,
    )


def test_write_last_run(tmp_path: Path) -> None:
    path = tmp_path / "data" / "last_run.json"
    daemon.write_last_run(path, SyncStats(uploaded=2))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["stats"]["uploaded"] == 2


def test_daemon_runs_once_and_stops(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    stop = threading.Event()

    class FakeSyncer:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run_once(self):
            stop.set()
            return SyncStats(uploaded=1)

    monkeypatch.setattr(daemon, "Syncer", FakeSyncer)
    daemon.run_daemon(config, object(), object(), stop)
    data = json.loads(config.last_run_path.read_text(encoding="utf-8"))
    assert data["stats"]["uploaded"] == 1
