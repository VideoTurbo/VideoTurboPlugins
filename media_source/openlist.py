"""OpenList 插件（https://github.com/OpenListTeam/OpenList）。

把 OpenList（也兼容 AList v3）挂载的网盘作为 VideoTurbo 的媒体库来源：主程序通过 OpenList 浏览目录、
列出影片、读写字幕和海报，GPU 机器通过 OpenList 的下载链接（/d/）直接下载影片抽音频。存储没开代理时，
OpenList 把下载链接 302 跳转到网盘直链，影片不经过 OpenList；开了 Web 代理或本地代理时由 OpenList 转发。

登录方式三选一：OpenList“设置 → 其他”中的令牌、用户名和密码（登录后得到的令牌保存在插件状态里，
过期后自动重新登录），或游客（不登录，游客通常没有写入权限，无法发布字幕）。

列目录一律使用 OpenList 的目录缓存，不强制刷新：网盘里在 OpenList 之外新增的文件，要等 OpenList
的缓存过期后才会被扫描到。通过本插件写入的字幕、改名会由 OpenList 自己更新缓存。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from video_turbo.media_sources.base import (
    SMALL_FILE_LIMIT,
    MediaInput,
    MediaSourcePlugin,
    SourceEntry,
)
from video_turbo.plugins.base import (
    CredentialField,
    PluginCallError,
    PluginConfigError,
    normalize_base_url,
    response_error,
)

# 适配的 VideoTurbo 媒体库来源插件接口版本（4 起字段支持 visible_when）。
PLUGIN_API = 4

SERVICE = "OpenList"
# 请求 OpenList 和下载影片时使用同一个 User-Agent：部分网盘的直链与获取直链时的 User-Agent 绑定。
USER_AGENT = "VideoTurbo-OpenList/0.1"
# OpenList（沿用 AList）登录接口对密码做的固定加盐 SHA-256，见 internal/model/user.go 的 StaticHash。
_PASSWORD_SALT = "https://github.com/alist-org/alist"
_MAX_REDIRECTS = 5

AUTH_TOKEN = "token"
AUTH_PASSWORD = "password"
AUTH_GUEST = "guest"

_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?")

# 插件图标：OpenList 的标志（https://github.com/OpenListTeam/Logo）。
_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
    '<path fill="#38bdf8" d="M244.57,776.75c-10.1,0-20.31-2.78-29.46-8.59-25.63-16.3-33.2-50.29-16.9'
    "-75.92l201.93-317.6c16.3-25.63,50.29-33.2,75.92-16.9,25.63,16.3,33.2,50.29,16.9,75.92l-201.93,"
    '317.6c-10.48,16.48-28.28,25.5-46.46,25.5Z"/>'
    '<path fill="#99f6e4" d="M509.93,907.83c-35.01,0-67.29-4.84-91.84-13.86-15.63-5.74-27.82-18.25-'
    "33.15-34.03s-3.23-33.12,5.72-47.16l174.43-273.77c16.32-25.62,50.32-33.15,75.94-16.83,25.62,16.32,"
    "33.15,50.32,16.83,75.94l-126.68,198.82c25.39-1.89,54.61-7.42,84.56-19.13,71.29-27.87,127.46-"
    "82.26,158.15-153.15,30.53-70.52,31.94-147.78,3.98-217.56-28.43-70.95-82.76-126.78-152.98-157.2-"
    "70.23-30.42-147.71-31.59-218.17-3.27-73.46,29.52-126.75,82.48-158.4,157.4-11.82,27.98-44.08,41.08"
    "-72.07,29.27-27.98-11.82-41.08-44.08-29.27-72.07,42.86-101.46,118.49-176.38,218.71-216.66,49.54-"
    "19.91,101.59-29.4,154.67-28.2,51.11,1.15,100.99,12.12,148.25,32.6,47.14,20.42,89.3,49.27,125.31,"
    "85.75,37.27,37.75,66.22,81.98,86.05,131.46,19.64,49.01,28.94,100.74,27.64,153.75-1.26,51.15-12.28,"
    "101.08-32.77,148.42-20.54,47.45-49.51,89.78-86.11,125.81-38.15,37.56-82.88,66.53-132.94,86.09-42.5,"
    '16.61-89.11,26.08-134.81,27.39-3.71.11-7.4.16-11.05.16Z"/>'
    "</svg>"
)


class OpenListError(PluginCallError):
    """OpenList 接口返回了错误（响应体中的 code 不是 200）。"""

    def __init__(self, code: object, detail: str) -> None:
        self.code = code
        self.detail = detail
        if code == 401:
            text = f"OpenList 拒绝了登录信息：{detail}。请在插件设置中检查登录方式和账号。"
        elif code == 403:
            text = f"OpenList 账号没有权限：{detail}"
        else:
            text = f"OpenList 返回错误（{code}）：{detail}"
        super().__init__(text)

    @property
    def missing(self) -> bool:
        """文件或目录不存在（OpenList 用 500 加错误文字表示）。"""
        detail = self.detail.lower()
        return "not found" in detail or "not a folder" in detail or "not a dir" in detail


def _clean_path(path: str) -> str:
    """来源内的绝对路径：以 / 开头，去掉多余的 /、. 和 ..。"""
    path = posixpath.normpath("/" + str(path or "").strip("/"))
    return "/" + path.lstrip("/")


def _encode_path(path: str) -> str:
    return quote(path, safe="/")


def _mtime_ns(value: object) -> int:
    """OpenList 返回的 RFC 3339 时间（小数秒最多 9 位）→ 纳秒；没有时间（0001 年）时为 0。"""
    match = _TIME.fullmatch(str(value or "").strip())
    if not match:
        return 0
    zone = match.group(3) or "Z"
    moment = datetime.fromisoformat(match.group(1) + ("+00:00" if zone == "Z" else zone))
    if moment.year < 1971:
        return 0
    fraction = (match.group(2) or "")[:9].ljust(9, "0")
    return int(moment.timestamp()) * 1_000_000_000 + int(fraction)


def _file_key(item: dict[str, Any]) -> str | None:
    """文件内容的哈希（网盘提供时）：改名、移动后不变，用来认出改名的影片。"""
    hashes = item.get("hash_info")
    if not isinstance(hashes, dict):
        try:
            hashes = json.loads(item.get("hashinfo") or "null")
        except (TypeError, ValueError):
            hashes = None
    if not isinstance(hashes, dict):
        return None
    for kind in sorted(str(key) for key in hashes):
        value = hashes.get(kind)
        if isinstance(value, str) and value.strip():
            return f"{kind}:{value.strip().lower()}"
    return None


class OpenListSource(MediaSourcePlugin):
    id = "openlist"
    label = "OpenList"
    version = "0.2.0"
    homepage = "https://github.com/OpenListTeam/OpenList"
    icon = _ICON
    description = "把 OpenList（或 AList v3）挂载的网盘作为媒体库来源。"
    settings_fields = (
        CredentialField(
            "url",
            "OpenList 地址",
            secret=False,
            placeholder="http://192.168.1.10:5244",
            help=(
                "主程序和 GPU 机器都用这个地址访问 OpenList，请填局域网或公网地址，不要填 127.0.0.1 或容器名。"
                "影片通过 OpenList 的下载链接下载：存储没开 Web 代理和本地代理时，OpenList 302 跳转到网盘直链，"
                "影片不经过 OpenList；开了代理时由 OpenList 转发。"
            ),
            binds_secrets=True,
        ),
        CredentialField(
            "auth",
            "登录方式",
            type="select",
            options=(
                (AUTH_TOKEN, "令牌"),
                (AUTH_PASSWORD, "用户名和密码"),
                (AUTH_GUEST, "游客（不登录）"),
            ),
            default=AUTH_TOKEN,
            help="游客通常没有写入权限，无法发布字幕。",
        ),
        CredentialField(
            "token",
            "令牌",
            help="OpenList“设置 → 其他”中的令牌，拥有管理员权限。",
            visible_when={"auth": (AUTH_TOKEN,)},
        ),
        CredentialField(
            "username",
            "用户名",
            secret=False,
            help="账号需要有读取、写入和改名权限，不能开启两步验证。",
            visible_when={"auth": (AUTH_PASSWORD,)},
        ),
        CredentialField(
            "password",
            "密码",
            trim=False,
            visible_when={"auth": (AUTH_PASSWORD,)},
        ),
        CredentialField(
            "folder_password",
            "目录密码",
            required=False,
            trim=False,
            help="媒体库所在目录在 OpenList 中设置了访问密码时填写。",
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client.headers["User-Agent"] = USER_AGENT
        self._base = str(self.settings.get("url") or "").rstrip("/")
        # 本实例内列过的目录：{目录: [条目, ...]}，不存在的目录为 None。实例按次创建，写入或改名后清掉相关目录。
        self._listings: dict[str, list[dict[str, Any]] | None] = {}

    # ---- 插件设置 ----

    @classmethod
    def validate_settings(cls, settings: dict[str, Any]) -> dict[str, Any]:
        settings = dict(settings)
        settings["url"] = normalize_base_url(str(settings.get("url") or ""), "OpenList 地址")
        mode = settings.get("auth") or AUTH_TOKEN
        if mode == AUTH_TOKEN and not settings.get("token"):
            raise PluginConfigError("登录方式为“令牌”时需要填写令牌。")
        if mode == AUTH_PASSWORD and not (settings.get("username") and settings.get("password")):
            raise PluginConfigError("登录方式为“用户名和密码”时需要填写用户名和密码。")
        return settings

    @property
    def _mode(self) -> str:
        return str(self.settings.get("auth") or AUTH_TOKEN)

    # ---- 接口调用 ----

    def _token(self) -> str:
        if self._mode == AUTH_GUEST:
            return ""
        if self._mode == AUTH_TOKEN:
            return str(self.settings.get("token") or "")
        token = self.state.get("token")
        if isinstance(token, str) and token:
            return token
        return self._login()

    def _login(self) -> str:
        password = str(self.settings.get("password") or "")
        hashed = hashlib.sha256(f"{password}-{_PASSWORD_SALT}".encode()).hexdigest()
        try:
            data = self._call(
                "/api/auth/login/hash",
                {"username": str(self.settings.get("username") or ""), "password": hashed},
                auth=False,
            )
        except OpenListError as exc:
            if exc.code == 402:
                raise PluginCallError("该 OpenList 账号开启了两步验证，请改用令牌登录。") from exc
            if exc.code == 401:
                raise PluginCallError(
                    f"登录 OpenList 失败：用户名或密码错误（{exc.detail}）。"
                ) from exc
            if exc.code == 429:
                raise PluginCallError("登录 OpenList 失败的次数过多，请稍后再试。") from exc
            raise
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise PluginCallError("OpenList 登录成功但没有返回令牌。")
        self.save_state({"token": token})
        return token

    def _call(
        self,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> Any:
        """调用 OpenList 接口，返回响应中的 data。用户名密码登录的令牌失效时重新登录一次。"""
        for attempt in range(2):
            request_headers = dict(headers or {})
            if auth and (token := self._token()):
                request_headers["Authorization"] = token
            url = self._base + endpoint
            if content is not None:
                response = self.client.request(
                    method, url, content=content, headers=request_headers
                )
            elif payload is None:
                response = self.client.request(method, url, headers=request_headers)
            else:
                response = self.client.request(method, url, json=payload, headers=request_headers)
            if error := response_error(response, SERVICE):
                raise error
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, dict) or "code" not in body:
                raise PluginCallError("OpenList 返回的内容无法识别，请检查 OpenList 地址是否正确。")
            code = body.get("code")
            if code == 200:
                return body.get("data")
            if (
                code == 401
                and auth
                and attempt == 0
                and self._mode == AUTH_PASSWORD
                and self.state.get("token")
            ):
                self.save_state({"token": None})
                continue
            raise OpenListError(code, str(body.get("message") or ""))
        raise AssertionError("unreachable")

    def _folder_password(self) -> str:
        return str(self.settings.get("folder_password") or "")

    def _list(self, directory: str) -> list[dict[str, Any]]:
        """目录下一层的原始条目（使用 OpenList 的目录缓存）；目录不存在时抛出 FileNotFoundError。"""
        directory = _clean_path(directory)
        if directory in self._listings:
            cached = self._listings[directory]
            if cached is None:
                raise FileNotFoundError(directory)
            return cached
        try:
            data = self._call(
                "/api/fs/list",
                {
                    "path": directory,
                    "password": self._folder_password(),
                    "page": 1,
                    "per_page": 0,
                    "refresh": False,
                },
            )
        except OpenListError as exc:
            if exc.missing:
                self._listings[directory] = None
                raise FileNotFoundError(directory) from exc
            raise
        content = data.get("content") if isinstance(data, dict) else None
        items = [
            item
            for item in content or []
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"] not in {"", ".", ".."}
            and "/" not in item["name"]
        ]
        self._listings[directory] = items
        return items

    def _item(self, path: str) -> dict[str, Any] | None:
        """文件或目录的原始条目，从上级目录的列表里找，避免 fs/get 每次都向网盘获取直链。"""
        path = _clean_path(path)
        if path == "/":
            return {"name": "", "is_dir": True}
        directory, name = posixpath.split(path)
        try:
            items = self._list(directory)
        except FileNotFoundError:
            return None
        return next((item for item in items if item["name"] == name), None)

    def _forget(self, *paths: str) -> None:
        for path in paths:
            self._listings.pop(posixpath.dirname(_clean_path(path)), None)

    @staticmethod
    def _entry(path: str, item: dict[str, Any]) -> SourceEntry:
        is_dir = bool(item.get("is_dir"))
        try:
            size = 0 if is_dir else max(int(item.get("size") or 0), 0)
        except (TypeError, ValueError):
            size = 0
        return SourceEntry(
            path=path,
            is_dir=is_dir,
            size=size,
            mtime_ns=_mtime_ns(item.get("modified")),
            file_key=None if is_dir else _file_key(item),
        )

    # ---- 下载地址 ----

    def _link(self, path: str) -> MediaInput:
        """OpenList 下载链接（/d/）：OpenList 按存储设置 302 跳转到网盘直链或代理转发。

        跳转时 OpenList 按下载方（GPU 机器或主程序）自己的 IP 和 User-Agent 获取网盘直链。
        """
        path = _clean_path(path)
        item = self._item(path)
        if item is None or item.get("is_dir"):
            raise FileNotFoundError(path)
        url = f"{self._base}/d{_encode_path(path)}"
        sign = item.get("sign")
        if isinstance(sign, str) and sign:
            url += "?sign=" + quote(sign, safe="")
        return MediaInput(url, {"User-Agent": USER_AGENT})

    # ---- MediaSource ----

    def list_dir(self, path: str) -> list[SourceEntry]:
        directory = _clean_path(path)
        return [
            self._entry(posixpath.join(directory, item["name"]), item)
            for item in self._list(directory)
            if not item["name"].startswith(".")
        ]

    def stat(self, path: str) -> SourceEntry | None:
        path = _clean_path(path)
        item = self._item(path)
        return None if item is None else self._entry(path, item)

    def read_bytes(self, path: str, limit: int = SMALL_FILE_LIMIT) -> bytes:
        target = self._link(path)
        url = httpx.URL(target.url)
        # 下载链接不带 Authorization，可以跟随 OpenList 跳转到网盘直链。
        for _ in range(_MAX_REDIRECTS + 1):
            with self.client.stream("GET", url, headers=dict(target.headers)) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        break
                    url = url.join(location)
                    if url.scheme not in {"http", "https"}:
                        break
                    continue
                if response.status_code == 404:
                    raise FileNotFoundError(path)
                if error := response_error(response, SERVICE):
                    raise error
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > limit:
                    raise ValueError(f"{posixpath.basename(path)} 超过 {limit} 字节。")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise ValueError(f"{posixpath.basename(path)} 超过 {limit} 字节。")
                return bytes(data)
        raise PluginCallError(f"下载 {posixpath.basename(path)} 时跳转次数过多或跳转地址无效。")

    def write_bytes(self, path: str, data: bytes, *, overwrite: bool) -> None:
        path = _clean_path(path)
        if not overwrite and self._item(path) is not None:
            raise FileExistsError(path)
        try:
            # OpenList 上传时自动创建缺少的上级目录，并更新目录缓存。
            self._call(
                "/api/fs/put",
                method="PUT",
                content=data,
                headers={
                    "File-Path": _encode_path(path),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(data)),
                    "As-Task": "false",
                    "Overwrite": "true" if overwrite else "false",
                },
            )
        except OpenListError as exc:
            if "exist" in exc.detail.lower() and not overwrite:
                raise FileExistsError(path) from exc
            raise
        finally:
            self._forget(path)

    def rename(self, source: str, target: str) -> None:
        source, target = _clean_path(source), _clean_path(target)
        if source == target:
            return
        if self._item(source) is None:
            raise FileNotFoundError(source)
        if self._item(target) is not None:
            raise FileExistsError(target)
        source_dir, source_name = posixpath.split(source)
        target_dir, target_name = posixpath.split(target)
        try:
            if source_dir == target_dir:
                self._rename(source, target_name)
                return
            if self._item(target_dir) is None:
                self._call("/api/fs/mkdir", {"path": target_dir})
            # 先在原目录改名再移动；原目录里已有同名文件时，先移动再在目标目录改名。
            if source_name == target_name:
                self._move(source_dir, target_dir, source_name)
            elif self._item(posixpath.join(source_dir, target_name)) is None:
                self._rename(source, target_name)
                self._move(source_dir, target_dir, target_name)
            elif self._item(posixpath.join(target_dir, source_name)) is None:
                self._move(source_dir, target_dir, source_name)
                self._rename(posixpath.join(target_dir, source_name), target_name)
            else:
                raise PluginCallError(
                    f"无法把 {source} 移动为 {target}：两个目录里都已有同名文件。"
                )
        finally:
            self._forget(source, target, target_dir)

    def _rename(self, path: str, name: str) -> None:
        self._call("/api/fs/rename", {"path": path, "name": name, "overwrite": False})
        self._forget(path)

    def _move(self, source_dir: str, target_dir: str, name: str) -> None:
        data = self._call(
            "/api/fs/move",
            {"src_dir": source_dir, "dst_dir": target_dir, "names": [name], "overwrite": False},
        )
        self._forget(posixpath.join(source_dir, name), posixpath.join(target_dir, name))
        if isinstance(data, dict) and data.get("tasks"):
            raise PluginCallError(
                "源目录和目标目录不在 OpenList 的同一个存储里，OpenList 已把移动作为后台任务执行；"
                "任务完成后重新扫描媒体库即可。"
            )

    def media_input(self, path: str) -> MediaInput:
        return self._link(path)

    def test(self) -> str:
        data = self._call("/api/me", method="GET")
        username = data.get("username") if isinstance(data, dict) else ""
        self._list("/")
        if self._mode == AUTH_GUEST:
            return "已连接 OpenList（游客）。"
        return f"已连接 OpenList，账号：{username or '未知'}。"
