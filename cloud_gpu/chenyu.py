"""晨羽智云插件（https://www.chenyu.cn）。

让 VideoTurbo 管理晨羽智云上的 GPU 实例。
使用前在晨羽智云控制台「API管理」创建 API Key，填到 VideoTurbo 的平台授权里。
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from video_turbo.cloud.base import (
    CAP_DESTROY,
    CAP_IDLE_CLOSE,
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
    GpuOffer,
    InstanceStatus,
    LaunchMethod,
    LaunchTarget,
    SshInfo,
    instance_status_text,
)

# 适配的 VideoTurbo 云平台插件接口版本。
PLUGIN_API = 1

API_BASE = os.environ.get("VIDEO_TURBO_CHENYU_API_BASE", "https://www.chenyu.cn/api/open/v2")

_STATUS_DETAIL = {
    0: "已创建",
    23: "异常",
    27: "重启中",
}
_STATUS: dict[int, InstanceStatus] = {
    2: "running",
    0: "starting",
    1: "starting",
    24: "starting",
    27: "starting",
    3: "stopping",
    21: "stopping",
    4: "stopped",
    22: "stopped",
    23: "stopped",
}
_AUTH_KEYWORDS = ("api key", "apikey", "认证", "鉴权", "token", "权限", "unauthorized")
_FATAL_KEYWORDS = ("余额", "欠费", "参数")
_PAGE_SIZE = 100
_MAX_PAGES = 20


# 插件图标：晨羽智云的站点图标（64×64 PNG）。
_ICON = (
    "data:image/png;base64,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"
    "Jggg=="
)


class ChenyuProvider(CloudProvider):
    id = "chenyu"
    label = "晨羽智云"
    homepage = "https://www.chenyu.cn/console/login?invitationCode=IZUKW8"
    icon = _ICON
    version = "0.1.0"
    capabilities = frozenset(
        {CAP_LIST, CAP_SHUTDOWN, CAP_DESTROY, CAP_SAVE_IMAGE, CAP_IDLE_CLOSE, CAP_LAUNCH, CAP_SSH}
    )
    credential_fields = (
        CredentialField(
            "api_key",
            "API Key",
            placeholder="sk-…",
            help="在晨羽智云控制台「API管理」创建：https://www.chenyu.cn/console/apiServer",
        ),
    )
    idle_close_choices = (10, 30, 60, 120)
    launch_methods = (
        LaunchMethod("startup", LAUNCH_METHOD_LABELS["startup"], "实例", creates_instance=False),
        LaunchMethod("image", LAUNCH_METHOD_LABELS["image"], "私有镜像"),
        LaunchMethod("pod", LAUNCH_METHOD_LABELS["pod"], "Pod", searchable=True),
    )
    gpu_counts = (1, 2, 4, 8)

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        key = (self.credentials.get("api_key") or "").strip()
        if not key:
            raise CloudError(CloudErrorKind.AUTH, "未填写晨羽智云 API Key。")
        try:
            response = await self._client.request(
                method,
                API_BASE + path,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.TimeoutException as exc:
            raise CloudError(CloudErrorKind.TRANSIENT, "请求晨羽智云超时。") from exc
        except httpx.HTTPError as exc:
            raise CloudError(CloudErrorKind.TRANSIENT, f"无法连接晨羽智云：{exc}") from exc
        text = response.text[:300]
        if response.status_code in (401, 403):
            raise CloudError(
                CloudErrorKind.AUTH, f"API Key 无效或无权限（HTTP {response.status_code}）。"
            )
        if response.status_code == 429:
            raise CloudError(CloudErrorKind.RATE_LIMIT, "请求过于频繁（HTTP 429）。")
        if response.status_code >= 500:
            raise CloudError(
                CloudErrorKind.TRANSIENT, f"晨羽智云服务异常（HTTP {response.status_code}）：{text}"
            )
        if response.status_code >= 400:
            raise CloudError(
                CloudErrorKind.FATAL, f"晨羽智云拒绝请求（HTTP {response.status_code}）：{text}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise CloudError(CloudErrorKind.TRANSIENT, "晨羽智云返回的不是 JSON。") from exc
        if not isinstance(body, dict):
            raise CloudError(CloudErrorKind.TRANSIENT, "晨羽智云返回格式异常。")
        if body.get("code") != 0:
            message = str(body.get("msg") or body.get("message") or f"code={body.get('code')}")
            lowered = message.lower()
            if body.get("error_code") == "GPU_CAPACITY_EXHAUSTED":
                kind = CloudErrorKind.OUT_OF_STOCK
            elif any(word in lowered for word in _AUTH_KEYWORDS):
                kind = CloudErrorKind.AUTH
            elif any(word in message for word in _FATAL_KEYWORDS):
                kind = CloudErrorKind.FATAL
            else:
                kind = CloudErrorKind.TRANSIENT
            raise CloudError(kind, f"晨羽智云：{message}")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _instance(raw: dict[str, Any]) -> CloudInstance:
        code = raw.get("status")
        status = _STATUS.get(code, "unknown") if isinstance(code, int) else "unknown"

        def timestamp(value: Any) -> float | None:
            return float(value) if isinstance(value, (int, float)) and value > 0 else None

        ssh = None
        for service in raw.get("server_map") or []:
            info = service.get("ssh_info") if isinstance(service, dict) else None
            if service.get("port_type") == "ssh" and isinstance(info, dict) and info.get("host"):
                ssh = SshInfo(
                    host=str(info["host"]),
                    port=int(info.get("port") or 22),
                    username=str(info.get("username") or "root"),
                    password=str(info["password"]) if info.get("password") else None,
                )
                break

        return CloudInstance(
            id=str(raw.get("instance_uuid") or ""),
            gpu_id=str(raw.get("gpu_uuid") or ""),
            ssh=ssh,
            title=str(raw.get("title") or ""),
            status=status,
            status_text=instance_status_text(
                status, _STATUS_DETAIL.get(code, f"未知状态 {code}" if status == "unknown" else "")
            ),
            gpu_name=str(raw.get("gpu_name") or ""),
            gpu_count=int(raw.get("gpu_nums") or 0),
            started_at=timestamp(raw.get("start_time")),
            created_at=timestamp(raw.get("create_time")),
        )

    async def list_instances(self) -> list[CloudInstance]:
        # 文档未说明分页参数，示例使用 page/page_size；平台忽略时第一页即为全部。
        found: dict[str, CloudInstance] = {}
        for page in range(1, _MAX_PAGES + 1):
            data = await self._request(
                "GET", "/instance/list", params={"page": page, "page_size": _PAGE_SIZE}
            )
            items = data.get("instance_list") or []
            new = 0
            for raw in items:
                if isinstance(raw, dict) and raw.get("instance_uuid"):
                    instance = self._instance(raw)
                    if instance.id not in found:
                        found[instance.id] = instance
                        new += 1
            total = data.get("total")
            if not new or not isinstance(total, int) or len(found) >= total:
                break
        return list(found.values())

    async def shutdown(self, instance_id: str) -> None:
        await self._request("POST", "/instance/shutdown", json={"instance_uuid": instance_id})

    async def destroy(self, instance_id: str) -> None:
        await self._request("POST", "/instance/destroy", json={"instance_uuid": instance_id})

    async def save_image(self, instance_id: str, image_name: str = "") -> None:
        body = {"instance_uuid": instance_id}
        if image_name.strip():
            body["image_name"] = image_name.strip()
        await self._request("POST", "/instance/save_image", json=body)

    async def set_idle_close(self, instance_id: str, minutes: int) -> None:
        if minutes and minutes not in self.idle_close_choices:
            raise CloudError(
                CloudErrorKind.FATAL, f"晨羽智云空闲关机只支持 {self.idle_close_choices} 分钟。"
            )
        await self._request(
            "POST",
            "/instance/set_idle_close",
            json={"instance_uuid": instance_id, "idle_period_minutes": minutes, "destroy": False},
        )

    async def gpu_offers(self) -> list[GpuOffer]:
        data = await self._request("GET", "/gpu/list")
        items = data.get("gpu_list") or []
        if isinstance(items, dict):  # 文档写成对象，实际返回数组；两种都接受。
            items = [items]
        offers = []
        for item in items:
            if not isinstance(item, dict) or not item.get("gpu_uuid"):
                continue
            price = (item.get("price") or {}).get("hour")
            status = item.get("status")
            offers.append(
                GpuOffer(
                    id=str(item["gpu_uuid"]),
                    label=str(item.get("gpu_name") or item["gpu_uuid"]),
                    available=bool(status) if isinstance(status, int) else None,
                    price_per_hour=float(price) if isinstance(price, (int, float)) else None,
                )
            )
        return offers

    async def launch_targets(self, method: str, query: str = "") -> list[LaunchTarget]:
        if method == "startup":
            return [
                LaunchTarget(
                    id=instance.id,
                    label=instance.title or instance.id[:8],
                    detail=f"{instance.gpu_name or '未知 GPU'} · {instance.status_text}",
                )
                for instance in await self.list_instances()
                if instance.status == "stopped"
            ]
        if method == "image":
            data = await self._request("GET", "/image/private/list")
            targets = []
            for image in data.get("image_list") or []:
                if not isinstance(image, dict) or not image.get("uuid"):
                    continue
                size = image.get("size") or 0
                # 文档写 GB，实际返回字节。
                size_gb = size / 1024**3 if isinstance(size, (int, float)) and size > 1e6 else size
                ready = image.get("save_image_status") == 2
                targets.append(
                    LaunchTarget(
                        id=str(image["uuid"]),
                        label=str(image.get("title") or image["uuid"][:8]),
                        detail=f"{size_gb:.1f} GB" + ("" if ready else " · 未保存完成"),
                    )
                )
            return targets
        if method == "pod":
            data = await self._request(
                "GET", "/pod/list", params={"page": 1, "page_size": 30, "name": query}
            )
            targets = []
            for pod in data.get("pod_list") or []:
                if not isinstance(pod, dict) or not pod.get("uuid"):
                    continue
                tags = [tag for value in pod.get("pod_tag") or [] for tag in str(value).split()]
                for tag in tags or [""]:
                    targets.append(
                        LaunchTarget(
                            id=f"{pod['uuid']}|{tag}",
                            label=f"{pod.get('title') or pod['uuid'][:8]}{f' · {tag}' if tag else ''}",
                            detail=str(pod.get("remark") or "")[:80],
                        )
                    )
            return targets
        raise CloudError(CloudErrorKind.FATAL, f"未知的开机方式：{method}")

    async def launch(self, method: str, target_id: str, gpu_id: str, gpu_count: int) -> str | None:
        if method == "startup":
            await self._request(
                "POST",
                "/instance/startup",
                json={"instance_uuid": target_id, "gpu_uuid": gpu_id, "gpu_nums": gpu_count},
            )
            return target_id
        if method == "image":
            body = {"image_uuid": target_id, "gpu_uuid": gpu_id, "gpu_nums": gpu_count}
            data = await self._request("POST", "/instance/create_by_image", json=body)
        elif method == "pod":
            pod_uuid, _, tag = target_id.partition("|")
            body = {"pod_uuid": pod_uuid, "pod_tag": tag, "gpu_uuid": gpu_id, "gpu_nums": gpu_count}
            data = await self._request("POST", "/instance/create_by_pod", json=body)
        else:
            raise CloudError(CloudErrorKind.FATAL, f"未知的开机方式：{method}")
        instance_id = data.get("instance_uuid")
        return str(instance_id) if instance_id else None

    async def test(self) -> str:
        summary = await super().test()
        try:
            balance = await self._request("GET", "/balance/info")
        except CloudError:
            return summary
        parts = [
            f"{label} {balance[key]}"
            for key, label in (("balance", "充值余额"), ("card_balance", "算力卡余额"))
            if isinstance(balance.get(key), (int, float))
        ]
        return f"{summary}{'，'.join(parts)}。" if parts else summary
