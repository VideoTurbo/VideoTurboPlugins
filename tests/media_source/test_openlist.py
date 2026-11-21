"""OpenList 媒体库来源插件：用内存中的假 OpenList 验证接口调用方式。"""

import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from video_turbo.database import Database
from video_turbo.plugins import installer, registry
from video_turbo.plugins.base import PluginCallError, PluginConfigError, icon_uri

from media_source.openlist import USER_AGENT, OpenListSource, _mtime_ns

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://openlist.lan:5244"
TOKEN = "admin-token"
USERNAME = "video"
PASSWORD = " secret "
SALT = "https://github.com/alist-org/alist"
MODIFIED = "2024-05-01T12:00:00.123456789+08:00"


class FakeOpenList:
    """只实现插件用到的接口；文件和目录保存在内存中。"""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {
            "/movies/a.mp4": b"video-a",
            "/movies/.hidden.mp4": b"hidden",
            "/movies/a.ja.srt": b"1\n00:00:01,000 --> 00:00:02,000\nhi\n",
            "/movies/poster.jpg": b"poster",
        }
        self.dirs: set[str] = {"/", "/movies", "/shows"}
        self.tokens: set[str] = {TOKEN}
        self.guest = False
        self.sign = ""
        self.tasks = False
        self.requests: list[httpx.Request] = []
        self.logins = 0

    # ---- 辅助 ----

    def children(self, directory: str) -> list[dict]:
        prefix = directory.rstrip("/") + "/"
        names: dict[str, dict] = {}
        for path, data in self.files.items():
            if path.startswith(prefix) and "/" not in path[len(prefix) :]:
                names[path[len(prefix) :]] = self.obj(path, data)
        for path in self.dirs:
            if path != directory and path.startswith(prefix) and "/" not in path[len(prefix) :]:
                names[path[len(prefix) :]] = {
                    "name": path[len(prefix) :],
                    "size": 0,
                    "is_dir": True,
                    "modified": MODIFIED,
                    "sign": "",
                    "hash_info": None,
                }
        return list(names.values())

    def obj(self, path: str, data: bytes) -> dict:
        return {
            "name": path.rsplit("/", 1)[1],
            "size": len(data),
            "is_dir": False,
            "modified": MODIFIED,
            "created": MODIFIED,
            "sign": self.sign,
            "thumb": "",
            "type": 2,
            "hashinfo": "null",
            "hash_info": {"sha1": hashlib.sha1(data).hexdigest().upper()},
        }

    @staticmethod
    def ok(data=None) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "message": "success", "data": data})

    @staticmethod
    def fail(code: int, message: str) -> httpx.Response:
        return httpx.Response(200, json={"code": code, "message": message, "data": None})

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    # ---- 请求处理 ----

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "cdn.example":
            path = unquote(url.path)
            assert "authorization" not in request.headers
            return httpx.Response(200, content=self.files[path])
        if url.path.startswith("/d/"):
            assert "authorization" not in request.headers
            path = unquote(url.path[2:])
            if self.sign and url.params.get("sign") != self.sign:
                return httpx.Response(401)
            return httpx.Response(302, headers={"Location": f"https://cdn.example{url.path[2:]}"})
        endpoint = url.path
        if endpoint == "/api/auth/login/hash":
            body = json.loads(request.content)
            expected = hashlib.sha256(f"{PASSWORD}-{SALT}".encode()).hexdigest()
            if body != {"username": USERNAME, "password": expected}:
                return self.fail(401, "username or password is incorrect")
            self.logins += 1
            token = f"jwt-{self.logins}"
            self.tokens.add(token)
            return self.ok({"token": token})
        token = request.headers.get("authorization")
        if token is None and not self.guest:
            return self.fail(401, "Guest user is disabled, login please")
        if token is not None and token not in self.tokens:
            return self.fail(401, "token is expired")
        if endpoint == "/api/me":
            return self.ok({"username": "guest" if token is None else "admin", "base_path": "/"})
        if endpoint == "/api/fs/put":
            path = unquote(request.headers["file-path"])
            if request.headers.get("overwrite") == "false" and path in self.files:
                return self.fail(403, "file exists")
            self.files[path] = request.content
            parent = path.rsplit("/", 1)[0] or "/"
            while parent not in self.dirs:
                self.dirs.add(parent)
                parent = parent.rsplit("/", 1)[0] or "/"
            return self.ok()
        body = json.loads(request.content)
        if endpoint == "/api/fs/list":
            path = body["path"]
            if path not in self.dirs:
                return self.fail(500, "failed get objs: failed get dir: object not found")
            return self.ok(
                {
                    "content": self.children(path) or None,
                    "total": 0,
                    "write": True,
                    "provider": "Local",
                }
            )
        if endpoint == "/api/fs/get":
            path = body["path"]
            if path not in self.files:
                return self.fail(500, "object not found")
            return self.ok(self.obj(path, self.files[path]))
        if endpoint == "/api/fs/mkdir":
            self.dirs.add(body["path"])
            return self.ok()
        if endpoint == "/api/fs/rename":
            source = body["path"]
            target = source.rsplit("/", 1)[0] + "/" + body["name"]
            if self.exists(target):
                return self.fail(403, f"file [{body['name']}] exists")
            self.files[target] = self.files.pop(source)
            return self.ok()
        if endpoint == "/api/fs/move":
            (name,) = body["names"]
            if self.tasks:
                return self.ok(
                    {"message": "Successfully created 1 move task(s)", "tasks": [{"id": "1"}]}
                )
            self.files[f"{body['dst_dir']}/{name}"] = self.files.pop(f"{body['src_dir']}/{name}")
            return self.ok()
        raise AssertionError(f"未预期的请求：{request.method} {url}")

    def calls(self, endpoint: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == endpoint]


@pytest.fixture
def server() -> FakeOpenList:
    return FakeOpenList()


def source(server: FakeOpenList, state=None, on_state=None, **settings) -> OpenListSource:
    values = {"url": BASE, "auth": "token", "token": TOKEN, **settings}
    return OpenListSource(
        values, state=state, on_state=on_state, transport=httpx.MockTransport(server)
    )


# ---- 插件设置 ----


def test_validate_settings_checks_address_and_login_fields():
    settings = OpenListSource.validate_settings(
        {"url": "http://openlist.lan:5244/", "auth": "token", "token": "t"}
    )
    assert settings["url"] == BASE
    with pytest.raises(PluginConfigError, match="令牌"):
        OpenListSource.validate_settings({"url": BASE, "auth": "token"})
    with pytest.raises(PluginConfigError, match="用户名和密码"):
        OpenListSource.validate_settings({"url": BASE, "auth": "password", "username": USERNAME})
    with pytest.raises(PluginConfigError):
        OpenListSource.validate_settings({"url": "ftp://openlist.lan", "auth": "guest"})
    assert OpenListSource.validate_settings({"url": BASE, "auth": "guest"})["auth"] == "guest"


def test_settings_merge_rejects_new_address_with_kept_token():
    saved = OpenListSource.merge_settings({}, {"url": BASE, "auth": "token", "token": TOKEN})
    with pytest.raises(PluginConfigError):
        OpenListSource.merge_settings(saved, {"url": "http://evil.example"})


def test_login_fields_follow_selected_mode():
    fields = {field.key: field for field in OpenListSource.settings_fields}
    assert set(fields) == {"url", "auth", "token", "username", "password", "folder_password"}
    assert fields["token"].describe()["visible_when"] == {"auth": ["token"]}
    assert (
        fields["username"].visible_when
        == fields["password"].visible_when
        == {"auth": ("password",)}
    )
    # 没选过登录方式时按默认的“令牌”。
    with pytest.raises(PluginConfigError):
        OpenListSource.merge_settings({}, {"url": BASE})
    saved = OpenListSource.merge_settings({}, {"url": BASE, "token": TOKEN, "username": "u"})
    assert saved == {"url": BASE, "token": TOKEN}
    # 换成用户名和密码：令牌被清掉，用户名和密码必填。
    with pytest.raises(PluginConfigError):
        OpenListSource.merge_settings(saved, {"auth": "password", "username": USERNAME})
    switched = OpenListSource.merge_settings(
        saved, {"auth": "password", "username": USERNAME, "password": PASSWORD}
    )
    assert switched == {"url": BASE, "auth": "password", "username": USERNAME, "password": PASSWORD}
    # 游客：三项都不保存。
    assert OpenListSource.merge_settings(switched, {"auth": "guest"}) == {
        "url": BASE,
        "auth": "guest",
    }


# ---- 浏览与读取 ----


def test_list_dir_uses_cache_and_maps_entries(server):
    with source(server) as drive:
        entries = {entry.name: entry for entry in drive.list_dir("/movies")}
        assert set(entries) == {"a.mp4", "a.ja.srt", "poster.jpg"}
        video = entries["a.mp4"]
        assert video.path == "/movies/a.mp4" and video.size == 7 and not video.is_dir
        assert video.mtime_ns == 1714536000_123456789
        assert video.file_key == "sha1:" + hashlib.sha1(b"video-a").hexdigest()
        # 同一实例内 stat 复用已列出的目录。
        assert drive.stat("/movies/a.ja.srt").size == len(server.files["/movies/a.ja.srt"])
        assert drive.stat("/movies/missing.srt") is None
        assert drive.stat("/").is_dir and drive.stat("/shows").is_dir
        assert drive.list_dir("/shows") == []
        with pytest.raises(FileNotFoundError):
            drive.list_dir("/nope")
        assert drive.stat("/nope/a.mp4") is None
    listed = server.calls("/api/fs/list")
    assert [json.loads(request.content)["path"] for request in listed] == [
        "/movies",
        "/",
        "/shows",
        "/nope",
    ]
    first = json.loads(listed[0].content)
    assert first["refresh"] is False and first["per_page"] == 0
    assert listed[0].headers["authorization"] == TOKEN
    assert listed[0].headers["user-agent"] == USER_AGENT


def test_walk_finds_videos_recursively(server):
    server.files["/shows/s1/e1.mkv"] = b"e1"
    server.dirs.add("/shows/s1")
    with source(server) as drive:
        assert sorted(entry.path for entry in drive.walk("/", ["mp4", "mkv"])) == [
            "/movies/a.mp4",
            "/shows/s1/e1.mkv",
        ]


def test_read_bytes_follows_redirect_without_token_and_enforces_limit(server):
    server.sign = "abc:0"
    with source(server) as drive:
        assert drive.read_bytes("/movies/poster.jpg") == b"poster"
        with pytest.raises(ValueError):
            drive.read_bytes("/movies/a.mp4", limit=3)
        with pytest.raises(FileNotFoundError):
            drive.read_bytes("/movies/none.jpg")
    down = next(request for request in server.requests if request.url.path.startswith("/d/"))
    assert down.url.params["sign"] == "abc:0"


def test_media_input_and_download_use_openlist_link(server):
    server.sign = "s:0"
    server.files["/movies/中文 #1.mp4"] = b"x"
    with source(server) as drive:
        target = drive.media_input("/movies/中文 #1.mp4")
        assert target.url == f"{BASE}/d/movies/%E4%B8%AD%E6%96%87%20%231.mp4?sign=s%3A0"
        assert target.headers == {"User-Agent": USER_AGENT}
        assert drive.download("/movies/a.mp4").url == f"{BASE}/d/movies/a.mp4?sign=s%3A0"
        with pytest.raises(FileNotFoundError):
            drive.media_input("/movies/none.mp4")
        with pytest.raises(FileNotFoundError):
            drive.media_input("/movies")
    assert server.calls("/api/fs/get") == []


# ---- 写入与改名 ----


def test_write_bytes_respects_overwrite_and_refreshes_listing(server):
    with source(server) as drive:
        assert drive.stat("/movies/a.zh.srt") is None
        drive.write_bytes("/movies/a.zh.srt", b"zh", overwrite=False)
        assert drive.stat("/movies/a.zh.srt").size == 2
        puts = len(server.calls("/api/fs/put"))
        with pytest.raises(FileExistsError):
            drive.write_bytes("/movies/a.zh.srt", b"again", overwrite=False)
        assert len(server.calls("/api/fs/put")) == puts
        drive.write_bytes("/movies/a.zh.srt", b"new", overwrite=True)
        drive.write_bytes("/shows/new/e1.zh.srt", b"s", overwrite=False)
    assert server.files["/movies/a.zh.srt"] == b"new"
    assert server.files["/shows/new/e1.zh.srt"] == b"s"
    put = server.calls("/api/fs/put")[0]
    assert put.headers["file-path"] == "/movies/a.zh.srt" and put.headers["overwrite"] == "false"
    assert put.headers["authorization"] == TOKEN


def test_rename_in_place_and_across_directories(server):
    with source(server) as drive:
        drive.rename("/movies/a.ja.srt", "/movies/b.ja.srt")
        assert drive.stat("/movies/b.ja.srt") and drive.stat("/movies/a.ja.srt") is None
        drive.rename("/movies/b.ja.srt", "/archive/c.ja.srt")
        assert drive.stat("/archive/c.ja.srt") is not None
        with pytest.raises(FileExistsError):
            drive.rename("/movies/a.mp4", "/movies/poster.jpg")
        with pytest.raises(FileNotFoundError):
            drive.rename("/movies/none.mp4", "/movies/x.mp4")
    assert "/archive/c.ja.srt" in server.files and "/archive" in server.dirs


def test_rename_reports_background_move_task(server):
    server.tasks = True
    with source(server) as drive, pytest.raises(PluginCallError, match="后台任务"):
        drive.rename("/movies/a.mp4", "/shows/a.mp4")


# ---- 登录 ----


def test_password_login_saves_token_and_relogs_when_expired(server):
    saved = []
    with source(
        server,
        auth="password",
        token="",
        username=USERNAME,
        password=PASSWORD,
        on_state=saved.append,
    ) as drive:
        drive.list_dir("/movies")
        assert drive.state["token"] == "jwt-1"
    assert saved == [{"token": "jwt-1"}]
    server.tokens.discard("jwt-1")
    saved.clear()
    with source(
        server,
        state={"token": "jwt-1"},
        on_state=saved.append,
        auth="password",
        username=USERNAME,
        password=PASSWORD,
    ) as drive:
        assert drive.list_dir("/shows") == []
    assert saved == [{"token": None}, {"token": "jwt-2"}]
    assert server.logins == 2


def test_password_login_errors_are_explained(server):
    wrong = source(server, auth="password", username=USERNAME, password="wrong")
    with wrong, pytest.raises(PluginCallError, match="用户名或密码错误"):
        wrong.list_dir("/")

    def two_factor(request):
        return FakeOpenList.fail(402, "Invalid 2FA code")

    drive = OpenListSource(
        {"url": BASE, "auth": "password", "username": USERNAME, "password": PASSWORD},
        transport=httpx.MockTransport(two_factor),
    )
    with drive, pytest.raises(PluginCallError, match="两步验证"):
        drive.test()


def test_guest_and_token_errors(server):
    server.guest = True
    with source(server, auth="guest") as drive:
        assert drive.test() == "已连接 OpenList（游客）。"
    assert all("authorization" not in request.headers for request in server.requests)
    with source(server, token="wrong") as drive, pytest.raises(PluginCallError, match="登录信息"):
        drive.list_dir("/")


def test_test_reports_account(server):
    with source(server) as drive:
        assert drive.test() == "已连接 OpenList，账号：admin。"


def test_non_openlist_response_is_reported():
    drive = OpenListSource(
        {"url": BASE, "auth": "guest"},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>")),
    )
    with drive, pytest.raises(PluginCallError, match="无法识别"):
        drive.list_dir("/")


def test_mtime_parsing():
    assert _mtime_ns("0001-01-01T00:00:00Z") == 0
    assert _mtime_ns("") == 0 and _mtime_ns(None) == 0
    assert _mtime_ns("2024-05-01T04:00:00Z") == 1714536000 * 10**9
    assert _mtime_ns("2024-05-01T04:00:00.5Z") == 1714536000 * 10**9 + 500_000_000


# ---- 安装 ----


@pytest.fixture
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(_host: str, _port: int) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(installer, "_resolve", resolve)
    monkeypatch.delenv(installer.SAFE_MODE_ENV, raising=False)


@pytest.mark.anyio
@pytest.mark.usefixtures("_public_dns")
async def test_plugin_file_installs(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    db.initialize()
    content = (ROOT / "media_source" / "openlist.py").read_bytes()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    url = "https://example.com/openlist.py"
    try:
        preview = await installer.preview(url, transport)
        assert preview["version"] == OpenListSource.version
        result = await installer.install(db, url, preview["sha256"], transport)
        assert result["id"] == "openlist" and result["label"] == "OpenList"
        plugin = registry.get("media_source", "openlist")
        assert [field["key"] for field in plugin.describe()["settings_fields"]][:2] == [
            "url",
            "auth",
        ]
        assert icon_uri(plugin.icon).startswith("data:image/svg+xml")
    finally:
        installer.uninstall(db, "openlist")
    assert registry.get("media_source", "openlist") is None
