"""智川云插件（https://gpuhome.cc）。

让 VideoTurbo 管理智川云上的 GPU 实例。用智川云的手机号和密码登录，密码不会保存；
登录过期后重新登录即可。智川云没有官方开放接口，平台改版后可能需要更新插件。
"""

from __future__ import annotations

import shlex
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from video_turbo.cloud.base import (
    CAP_DESTROY,
    CAP_LAUNCH,
    CAP_LIST,
    CAP_SAVE_IMAGE,
    CAP_SHUTDOWN,
    CAP_SSH,
    LAUNCH_METHOD_LABELS,
    CloudError,
    CloudErrorKind,
    CloudInstance,
    CloudProvider,
    CredentialField,
    GpuConfiguration,
    GpuOffer,
    InstanceStatus,
    LaunchMethod,
    LaunchTarget,
    SshInfo,
    instance_status_text,
)

# 适配的 VideoTurbo 云平台插件接口版本。
PLUGIN_API = 1

API_BASE = "https://gpuhome.cc/api/v1"
_PAGE_SIZE = 100
_MAX_PAGES = 100
_STATUS_DETAIL = {
    1: "排队中",
    7: "运行异常",
    8: "已删除",
    9: "重置中",
    10: "重置完成",
    11: "克隆锁定中",
    12: "克隆中",
    14: "镜像拉取中",
    15: "远程镜像拉取中",
    16: "克隆排队中",
    17: "克隆锁定中",
    18: "私有镜像拉取异常",
}
_STATUS: dict[int, InstanceStatus] = {
    0: "starting",
    1: "starting",
    2: "starting",
    3: "running",
    4: "stopping",
    5: "stopped",
    14: "starting",
    15: "starting",
}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


# 插件图标：智川云的站点图标（64×64 PNG）。
_ICON = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAABGdBTUEAALGPC/xhBQ"
    "AAACBjSFJNAAB6JgAAgIQAAPoAAACA6AAAdTAAAOpgAAA6mAAAF3CculE8AAAAhGVYSWZNTQAqAAAACAAFARIAAw"
    "AAAAEAAQAAARoABQAAAAEAAABKARsABQAAAAEAAABSASgAAwAAAAEAAgAAh2kABAAAAAEAAABaAAAAAAAAAEgAAA"
    "ABAAAASAAAAAEAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAQKADAAQAAAABAAAAQAAAAAC1ay+zAAAACXBIWXMAAA"
    "sTAAALEwEAmpwYAAACyGlUWHRYTUw6Y29tLmFkb2JlLnhtcAAAAAAAPHg6eG1wbWV0YSB4bWxuczp4PSJhZG9iZT"
    "puczptZXRhLyIgeDp4bXB0az0iWE1QIENvcmUgNi4wLjAiPgogICA8cmRmOlJERiB4bWxuczpyZGY9Imh0dHA6Ly"
    "93d3cudzMub3JnLzE5OTkvMDIvMjItcmRmLXN5bnRheC1ucyMiPgogICAgICA8cmRmOkRlc2NyaXB0aW9uIHJkZj"
    "phYm91dD0iIgogICAgICAgICAgICB4bWxuczp0aWZmPSJodHRwOi8vbnMuYWRvYmUuY29tL3RpZmYvMS4wLyIKIC"
    "AgICAgICAgICAgeG1sbnM6ZXhpZj0iaHR0cDovL25zLmFkb2JlLmNvbS9leGlmLzEuMC8iPgogICAgICAgICA8dG"
    "lmZjpZUmVzb2x1dGlvbj43MjwvdGlmZjpZUmVzb2x1dGlvbj4KICAgICAgICAgPHRpZmY6UmVzb2x1dGlvblVuaX"
    "Q+MjwvdGlmZjpSZXNvbHV0aW9uVW5pdD4KICAgICAgICAgPHRpZmY6WFJlc29sdXRpb24+NzI8L3RpZmY6WFJlc2"
    "9sdXRpb24+CiAgICAgICAgIDx0aWZmOk9yaWVudGF0aW9uPjE8L3RpZmY6T3JpZW50YXRpb24+CiAgICAgICAgID"
    "xleGlmOlBpeGVsWERpbWVuc2lvbj45NjwvZXhpZjpQaXhlbFhEaW1lbnNpb24+CiAgICAgICAgIDxleGlmOkNvbG"
    "9yU3BhY2U+MTwvZXhpZjpDb2xvclNwYWNlPgogICAgICAgICA8ZXhpZjpQaXhlbFlEaW1lbnNpb24+OTY8L2V4aW"
    "Y6UGl4ZWxZRGltZW5zaW9uPgogICAgICA8L3JkZjpEZXNjcmlwdGlvbj4KICAgPC9yZGY6UkRGPgo8L3g6eG1wbW"
    "V0YT4KCzT7TAAAFERJREFUeAHtWgl4VUWWrrr7W7OShbAESASTsIawEwgRkiBBArywJxJ2FRVEFG3buGtr66hjq/"
    "TojO208w0Zu2112rbVAXXU0UYdR6MOIPsmSyDL2+5W/ddNnjAtgQjB7vm+3HCp++pW1T3nr7NU1TmEdF1dCHQh0I"
    "VAFwJdCHQh0IXA/1MEGCH0QkiXOtL5uXvfTNr1x2820AiLlwhlkkWI6HS0iUwEIto2EWyGOpSgSMZv2eYNGOEfEJ"
    "hNVOe3jbatfSWG9hZ+8/dox0s+jmRb4IgSyeHMcvpTPj5vw0uB96GMGJQKik73l6U+Sjau/Rzdz+vqEABef9Rr2+"
    "Iyl6D6CbFADGcKBLfdEuWEcYZwo05ivA1ngjMGEJySOkDI/Nm5W5l33oFpMIV61OGdgBtAO+15f/6bgygCZKdklH"
    "hsiQR7+J4y/dbO8+K8rVOHAAhaCrNEKdhi6H7KDCKbYA4k81nixHHC+LNktzJhOr9twstWADhYre0sMOIw3Qae8x"
    "vtKEDjkiNiDAETzOs5KAKvawOBlyLeyQYlxzPM5/eOcq8d89Da8MUHIGQK+CalIp9VzI1GDtnM3K5bTOTMgEbIAE"
    "oOCKjhksCfUYnfJuUoOwChLbQH0gtG8CCjjY0/C/W8n4WZpZxJ9INiUz42/y0xyvBpPHMVIRL1S9+wlX2vH7O08o"
    "KYxycdFePlWS/BLdk6qKEgmlKVaJnaa0+/e6OQkdKnp22ahihirkCpxOEQBUotmwpglcsIr2sdHAVmWWQClQSRqw"
    "ijQAhYUAWsyWBRhqJL0CVIC6VgXsKgKloqGEnD+IrFx7YMV273e1csvbkB43IDyN4YVzOOiqaVb1ySEj932B/otV"
    "OjZ2XotJcdUgEOgAHCKFdwENasN2uhSGN8t4TUW09Gm074VdUZ0jAE5vHCIgAAohPiEkwmcOOASwcrhES57oJhlZ"
    "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"
    "VORK5CYII="
)


class GpuhomeProvider(CloudProvider):
    id = "gpuhome"
    label = "智川云"
    homepage = "https://gpuhome.cc/auth/register?code=wszg01kv"
    icon = _ICON
    version = "0.1.0"
    credential_mode = "login"
    # 智川云的持久化数据盘。
    default_model_dir = "/root/rivermind-data/models"
    capabilities = frozenset(
        {CAP_LIST, CAP_SHUTDOWN, CAP_DESTROY, CAP_SAVE_IMAGE, CAP_LAUNCH, CAP_SSH}
    )
    credential_fields = (
        CredentialField("phone", "手机号", secret=False, placeholder="智川云注册手机号"),
        CredentialField(
            "password",
            "密码",
            placeholder="请输入智川云登录密码（仅用于登录，不保存）",
            trim=False,
            help="仅用于登录，不保存密码；登录后保存授权，失效后请重新登录。",
        ),
    )
    launch_methods = (
        LaunchMethod(
            "startup",
            LAUNCH_METHOD_LABELS["startup"],
            "实例",
            creates_instance=False,
            configurable_gpu=False,
        ),
        LaunchMethod(
            "image", LAUNCH_METHOD_LABELS["image"], "私有镜像", help="新建实例采用按量计费。"
        ),
        LaunchMethod(
            "public", LAUNCH_METHOD_LABELS["public"], "基础镜像", help="新建实例采用按量计费。"
        ),
        LaunchMethod(
            "community",
            LAUNCH_METHOD_LABELS["community"],
            "社区镜像",
            searchable=True,
            help="新建实例采用按量计费。",
        ),
    )
    gpu_counts = (1, 2, 4, 8)

    @classmethod
    def credentials_configured(cls, values: dict[str, str]) -> bool:
        return bool(values.get("phone") and values.get("token"))

    @classmethod
    def merge_credentials(cls, saved: dict[str, str], values: dict[str, str]) -> dict[str, str]:
        phone = (values.get("phone") or saved.get("phone") or "").strip()
        password = values.get("password") or ""
        if password:
            if not phone:
                raise CloudError(CloudErrorKind.AUTH, "请填写智川云手机号。")
            return {"phone": phone, "password": password}
        if phone == saved.get("phone") and cls.credentials_configured(saved):
            return {"phone": phone, "token": saved["token"]}
        raise CloudError(CloudErrorKind.AUTH, "请填写手机号和密码登录智川云。")

    async def prepare_credentials(self) -> dict[str, str]:
        await self._ensure_token()
        # 验证 Token 有效后才替换原授权；不会将密码写入数据库。
        await self.list_instances()
        return {"phone": self.credentials["phone"], "token": self.credentials["token"]}

    async def test(self) -> str:
        summary = await super().test()
        unavailable = summary + "余额暂不可用，请稍后重试。"
        try:
            account = await self._request("GET", "/user/user_info")
        except CloudError as exc:
            if exc.kind == CloudErrorKind.AUTH:
                raise
            return unavailable
        if not isinstance(account, dict):
            return unavailable
        amounts: dict[str, Decimal] = {}
        for key in ("account_remain_amount", "voucher_remain_amount"):
            value = account.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                amount = Decimal(str(value))
            except InvalidOperation:
                continue
            if amount.is_finite():
                amounts[key] = amount
        if not amounts:
            return unavailable
        parts = []
        if len(amounts) == 2:
            parts.append(f"账户总余额 ¥{sum(amounts.values()):.2f}")
        for key, label in (
            ("account_remain_amount", "现金余额"),
            ("voucher_remain_amount", "代金券余额"),
        ):
            if key in amounts:
                parts.append(f"{label} ¥{amounts[key]:.2f}")
        return summary + "，".join(parts) + "。"

    async def _ensure_token(self) -> str:
        if self.credentials.get("token"):
            return self.credentials["token"]
        phone, password = self.credentials.get("phone"), self.credentials.get("password")
        if not phone or not password:
            raise CloudError(CloudErrorKind.AUTH, "智川云尚未登录，请输入手机号和密码。")
        data = await self._send(
            "POST", "/user/password_login", json={"phone": phone, "pass_word": password}
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("token"), str)
            or not data["token"]
        ):
            raise CloudError(CloudErrorKind.AUTH, "智川云登录失败：未返回有效授权。")
        self.credentials["token"] = data["token"]
        self.credentials.pop("password", None)
        return data["token"]

    async def _request(self, method: str, path: str, *, json=None, params=None) -> Any:
        token = await self._ensure_token()
        return await self._send(method, path, json=json, params=params, token=token)

    async def _send(self, method: str, path: str, *, json=None, params=None, token=None) -> Any:
        headers = {"Accept-Language": "zh-CN"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._client.request(
                method,
                API_BASE + path,
                headers=headers,
                json=json,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise CloudError(CloudErrorKind.TRANSIENT, "无法连接智川云，请稍后重试。") from exc
        if response.status_code in (401, 403):
            raise CloudError(CloudErrorKind.AUTH, "智川云授权失效或无权限，请重新登录。")
        if response.status_code == 429:
            raise CloudError(CloudErrorKind.RATE_LIMIT, "智川云请求过于频繁，请稍后重试。")
        if response.status_code >= 500:
            raise CloudError(CloudErrorKind.TRANSIENT, "智川云服务暂时异常，请稍后重试。")
        if response.status_code >= 400:
            raise CloudError(
                CloudErrorKind.FATAL, f"智川云拒绝请求（HTTP {response.status_code}）。"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise CloudError(CloudErrorKind.TRANSIENT, "智川云返回的不是 JSON。") from exc
        if not isinstance(body, dict) or "code" not in body:
            raise CloudError(CloudErrorKind.TRANSIENT, "智川云返回格式异常。")
        if body["code"] not in (0, 200):
            message = str(body.get("message") or body.get("msg") or "请求失败")
            # 服务错误可进入日志或返回界面，绝不回显提交的凭据。
            secrets = [v for k, v in self.credentials.items() if k != "phone"]
            if json and "pass_word" in json:
                secrets.append(json["pass_word"])
            for secret in secrets:
                if secret:
                    message = message.replace(secret, "***")
            message = message[:300]
            if (
                body["code"] == 10010
                or any(
                    word in message.lower()
                    for word in ("token", "登录", "密码", "验证码", "权限", "unauthorized")
                )
                or path == "/user/password_login"
            ):
                kind = CloudErrorKind.AUTH
            elif any(word in message for word in ("频繁", "限流")):
                kind = CloudErrorKind.RATE_LIMIT
            elif body["code"] == 10003 or any(
                word in message for word in ("库存", "资源不足", "无卡", "售罄")
            ):
                kind = CloudErrorKind.OUT_OF_STOCK
            elif any(word in message for word in ("繁忙", "稍后", "超时", "系统异常")):
                kind = CloudErrorKind.TRANSIENT
            else:
                kind = CloudErrorKind.FATAL
            if kind == CloudErrorKind.AUTH:
                message = (
                    "登录失败，请检查账号密码。"
                    if path == "/user/password_login"
                    else ("授权失效或无权限，请重新登录。")
                )
            raise CloudError(kind, f"智川云：{message}")
        return body.get("data")

    async def _raw_instances(self) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for page in range(1, _MAX_PAGES + 1):
            data = await self._request(
                "GET", "/jupyter/list_notebook", params={"page_no": page, "page_size": _PAGE_SIZE}
            )
            if not isinstance(data, dict) or not isinstance(data.get("dataList"), list):
                raise CloudError(CloudErrorKind.TRANSIENT, "智川云实例列表格式异常。")
            items = data["dataList"]
            new = 0
            for item in items:
                if isinstance(item, dict) and item.get("instance_uuid"):
                    key = str(item["instance_uuid"])
                    new += key not in found
                    found[key] = item
            total = data.get("totalRecord")
            if isinstance(total, int) and len(found) >= total:
                return list(found.values())
            if not items or not new:
                if isinstance(total, int) and len(found) < total:
                    raise CloudError(CloudErrorKind.TRANSIENT, "智川云实例分页不完整，请重试。")
                return list(found.values())
            if total is None and len(items) < _PAGE_SIZE:
                return list(found.values())
        raise CloudError(CloudErrorKind.TRANSIENT, "智川云实例列表超过分页上限。")

    @staticmethod
    def _ssh(raw: dict[str, Any]) -> SshInfo | None:
        command = raw.get("ssh_command")
        if not isinstance(command, str) or not command:
            return None
        try:
            args = shlex.split(command)
            if not args or args[0] != "ssh":
                return None
            host, user, port = "", "root", 22
            index = 1
            while index < len(args):
                arg = args[index]
                if arg == "-p" and index + 1 < len(args):
                    port = int(args[index + 1])
                    index += 2
                    continue
                if arg.startswith("-p") and len(arg) > 2:
                    port = int(arg[2:])
                elif "@" in arg and not arg.startswith("-"):
                    user, host = arg.rsplit("@", 1)
                index += 1
            if host and 1 <= port <= 65535:
                return SshInfo(
                    host=host.strip("[]"),
                    port=port,
                    username=user,
                    password=str(raw["ssh_password"]) if raw.get("ssh_password") else None,
                )
        except (TypeError, ValueError):
            pass
        return None

    @classmethod
    def _instance(cls, raw: dict[str, Any]) -> CloudInstance:
        code = _integer(raw.get("status"), -1)
        status = _STATUS.get(code, "unknown")
        return CloudInstance(
            id=str(raw["instance_uuid"]),
            title=str(raw.get("nick_name") or raw.get("instance_name") or ""),
            status=status,
            status_text=instance_status_text(
                status, _STATUS_DETAIL.get(code, f"未知状态 {code}" if status == "unknown" else "")
            ),
            gpu_name=str(raw.get("product_name") or ""),
            gpu_count=_integer(raw.get("req_gpu_count")),
            region=str(raw.get("datacenter_name") or ""),
            gpu_id=f"{raw.get('product_name', '')}|{raw.get('datacenter_id', '')}",
            ssh=cls._ssh(raw),
        )

    async def list_instances(self) -> list[CloudInstance]:
        return [
            self._instance(raw)
            for raw in await self._raw_instances()
            if _integer(raw.get("status"), -1) != 8
        ]

    async def shutdown(self, instance_id: str) -> None:
        await self._request("POST", "/order/stop", json={"webide_instance_uuid": instance_id})

    async def destroy(self, instance_id: str) -> None:
        await self._request("POST", "/order/release", json={"webide_instance_uuid": instance_id})

    async def save_image(self, instance_id: str, image_name: str = "") -> None:
        name = image_name.strip() or "VideoTurbo"
        if len(name) > 10:
            raise CloudError(CloudErrorKind.FATAL, "智川云镜像名称最多 10 个字符。")
        raw = next(
            (r for r in await self._raw_instances() if r["instance_uuid"] == instance_id), None
        )
        if not raw or not raw.get("instance_id"):
            raise CloudError(CloudErrorKind.FATAL, "智川云实例不存在，请刷新后重试。")
        await self._request(
            "POST",
            "/images/self/save",
            json={
                "webide_run_id": raw["instance_id"],
                "image_name": name,
            },
        )

    async def _products(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/products/list")
        return self._array(data, "GPU 配置")

    @staticmethod
    def _array(data: Any, label: str) -> list[dict[str, Any]]:
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise CloudError(CloudErrorKind.TRANSIENT, f"智川云{label}格式异常。")
        return data

    @staticmethod
    def _gpu_id(product: dict, sku: dict) -> str:
        return f"{product.get('gpu_name', '')}|{sku.get('datacenter_id', '')}"

    @classmethod
    def _sku_options(cls, products: list[dict]) -> dict[tuple[str, int], tuple[dict, dict]]:
        options: dict[tuple[str, int], tuple[dict, dict]] = {}
        for product in products:
            if not product.get("gpu_memory"):
                continue
            for sku in product.get("product_skus") or []:
                count = _integer(sku.get("gpu_num"))
                if count <= 0 or not sku.get("gpu_sku_code") or not sku.get("datacenter_id"):
                    continue
                key = (cls._gpu_id(product, sku), count)
                if key not in options or _integer(sku.get("nodes_left")) > _integer(
                    options[key][1].get("nodes_left")
                ):
                    options[key] = (product, sku)
        return options

    @staticmethod
    def _configuration(product: dict, sku: dict) -> GpuConfiguration:
        count = _integer(sku.get("gpu_num"))
        value = sku.get("current_price")
        price = None
        if value is not None and not isinstance(value, bool):
            try:
                amount = Decimal(str(value))
                if amount.is_finite() and amount >= 0:
                    price = float(amount / 100)
            except InvalidOperation:
                pass
        detail = []
        cpu = _integer(sku.get("cpu_per_gpu") or product.get("cpu_per_gpu")) * count
        memory = _integer(sku.get("mem_per_gpu") or product.get("mem_per_gpu")) * count
        if cpu:
            detail.append(f"{cpu} 核 CPU")
        if memory:
            detail.append(f"{memory / 1024**3:g} GB 内存")
        for key, label in (("sys_disk_size", "系统盘"), ("data_disk_size", "数据盘")):
            size = _integer(product.get(key))
            if size:
                detail.append(f"{size / 1024**3:g} GB {label}")
        return GpuConfiguration(
            gpu_count=count,
            available=_integer(sku["nodes_left"]) > 0
            if sku.get("nodes_left") is not None
            else None,
            price_per_hour=price,
            detail=" · ".join(detail),
        )

    async def gpu_offers(self) -> list[GpuOffer]:
        # 按型号/地区合并；具体卡数在开机时匹配平台 SKU，不默认改动原实例配置。
        regions = self._array(await self._request("GET", "/data_center/tag"), "地区列表")
        names = {str(r.get("data_center_id")): str(r.get("data_center_name")) for r in regions}
        offers: dict[str, GpuOffer] = {}
        for (key, count), (product, sku) in self._sku_options(await self._products()).items():
            if key not in offers:
                offers[key] = GpuOffer(
                    id=key,
                    label=f"{product['gpu_name']} · "
                    + names.get(str(sku["datacenter_id"]), f"地区 {sku['datacenter_id']}"),
                )
            offers[key].configurations.append(self._configuration(product, sku))
        for offer in offers.values():
            offer.configurations.sort(key=lambda c: c.gpu_count)
            stocks = [c.available for c in offer.configurations]
            offer.available = True if True in stocks else (None if None in stocks else False)
            first = offer.configurations[0]
            # 保留通用单卡价格字段；界面优先使用各配置的合计价。
            if first.price_per_hour is not None:
                offer.price_per_hour = first.price_per_hour / first.gpu_count
        return list(offers.values())

    @staticmethod
    def _image_detail(row: dict) -> str:
        parts = []
        size = _integer(row.get("image_size"))
        if size:
            parts.append(f"{size / 1024**3:.1f} GB")
        elif row.get("size"):
            parts.append(str(row["size"]))
        if row.get("base_image_version"):
            parts.append(str(row["base_image_version"]))
        if row.get("cache_data_center"):
            parts.append(f"地区：{row['cache_data_center']}")
        if row.get("image_description") or row.get("notes"):
            parts.append(str(row.get("image_description") or row["notes"])[:120])
        return " · ".join(parts)

    async def launch_targets(self, method: str, query: str = "") -> list[LaunchTarget]:
        if method == "startup":
            return [
                LaunchTarget(
                    id=i.id,
                    label=i.title or i.id,
                    detail=" · ".join(
                        filter(
                            None,
                            (
                                f"{i.gpu_name} × {i.gpu_count}",
                                i.region,
                                "保持原配置和计费方式",
                            ),
                        )
                    ),
                )
                for i in await self.list_instances()
                if i.status == "stopped"
            ]
        if method == "image":
            rows = self._array(await self._request("GET", "/images/self/list"), "私有镜像")
            return [
                LaunchTarget(
                    id=str(r["image_id"]),
                    label=str(r.get("image_name") or r["image_id"]),
                    detail=self._image_detail(r),
                )
                for r in rows
                if r.get("image_id") and _integer(r.get("image_status")) == 2
            ]
        if method == "public":
            rows = self._array(
                await self._request("GET", "/images/public/display_list"), "基础镜像"
            )
            return [
                LaunchTarget(
                    id=str(v["image_id"]),
                    label=f"{r.get('display_name', '')} / {v.get('version', '')}",
                    detail=self._image_detail(v),
                )
                for r in rows
                for v in r.get("display_versions") or []
                if v.get("image_id")
            ]
        if method == "community":
            rows = self._array(await self._request("GET", "/images/community/all"), "社区镜像")
            q = query.casefold().strip()
            return [
                LaunchTarget(
                    id=str(r["image_id"]),
                    label=str(r.get("image_name") or r["image_id"]),
                    detail=self._image_detail(r),
                )
                for r in rows
                if r.get("image_id") and (not q or q in str(r.get("image_name", "")).casefold())
            ]
        raise CloudError(CloudErrorKind.FATAL, "智川云不支持这种开机方式。")

    async def launch(self, method: str, target_id: str, gpu_id: str, gpu_count: int) -> str | None:
        if method == "startup":
            await self._request("POST", "/order/start", json={"webide_instance_uuid": target_id})
            return target_id
        if method not in {"image", "public", "community"}:
            raise CloudError(CloudErrorKind.FATAL, "智川云不支持这种开机方式。")
        if _integer(target_id) <= 0:
            raise CloudError(CloudErrorKind.FATAL, "请选择有效的智川云镜像。")
        # 提交前核对目标仍存在，避免使用过时或已删除的镜像。
        if target_id not in {t.id for t in await self.launch_targets(method)}:
            raise CloudError(CloudErrorKind.FATAL, "智川云镜像已不可用，请重新选择。")
        option = self._sku_options(await self._products()).get((gpu_id, gpu_count))
        sku = option[1] if option else None
        if not sku or not sku.get("gpu_sku_code"):
            raise CloudError(CloudErrorKind.FATAL, "该地区不支持所选 GPU 数量，请重新选择配置。")
        data = await self._request(
            "POST",
            "/order/create",
            json={
                "product_sku_code": str(sku["gpu_sku_code"]),
                "pay_type": "usetime",
                "gpu_num": gpu_count,
                "image_id": 0 if method == "image" else int(target_id),
                "self_image_id": int(target_id) if method == "image" else 0,
            },
        )
        if isinstance(data, dict):
            instance_id = data.get("instance_uuid") or data.get("webide_instance_uuid")
            return str(instance_id) if instance_id else None
        return None  # 复用主控的新建实例核对，防止超时重试重复创建。
