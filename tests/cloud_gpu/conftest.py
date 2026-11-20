from pathlib import Path

import pytest
from video_turbo.config_store import SETTINGS_KEY
from video_turbo.database import Database
from video_turbo.orchestrator import auth
from video_turbo.plugins import registry

from cloud_gpu.chenyu import ChenyuProvider
from cloud_gpu.gpuhome import GpuhomeProvider


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch):
    """接口测试不涉及登录；两个插件按主控安装后的方式注册。"""
    monkeypatch.setattr(auth, "ENFORCE", False)
    for provider in (ChenyuProvider, GpuhomeProvider):
        registry.register("cloud", provider)
    yield
    for provider in (ChenyuProvider, GpuhomeProvider):
        registry.unregister("cloud", provider.id)


def prepare_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    monkeypatch.setenv("VIDEO_TURBO_DATABASE", str(tmp_path / "app.db"))
    monkeypatch.setenv("VIDEO_TURBO_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("VIDEO_TURBO_RESTORE_BACKUP", "")
    database = Database(tmp_path / "app.db")
    database.initialize()
    database.set_runtime_json(SETTINGS_KEY, {"scan": {"enabled": False}})
    return database
