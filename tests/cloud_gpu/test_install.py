"""两个插件文件都能通过主控的安装流程：下载、静态检查、核对 SHA-256 后加载、注册。"""

from pathlib import Path

import httpx
import pytest
from video_turbo.database import Database
from video_turbo.plugins import installer, registry
from video_turbo.plugins.base import icon_uri

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试不访问网络：插件地址解析到公网地址。"""

    async def resolve(_host: str, _port: int) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(installer, "_resolve", resolve)
    monkeypatch.delenv(installer.SAFE_MODE_ENV, raising=False)


@pytest.mark.parametrize(("name", "label"), [("chenyu", "晨羽智云"), ("gpuhome", "智川云")])
async def test_plugin_file_installs(tmp_path: Path, name: str, label: str) -> None:
    # conftest 直接注册了插件类；按地址安装前先移除，否则 id 被视为已占用。
    registry.unregister("cloud", name)
    db = Database(tmp_path / "app.db")
    db.initialize()
    source = (ROOT / "cloud_gpu" / f"{name}.py").read_bytes()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=source))
    url = f"https://example.com/{name}.py"
    try:
        preview = await installer.preview(url, transport)
        assert preview["version"]
        result = await installer.install(db, url, preview["sha256"], transport)
        assert result["id"] == name and label in result["label"]
        assert result["version"] == preview["version"]
        provider = registry.get("cloud", name)
        assert provider.describe()["fields"]
        assert icon_uri(provider.icon).startswith("data:image/")
    finally:
        installer.uninstall(db, name)
    assert registry.get("cloud", name) is None
