import json

import httpx
import pytest
from video_turbo.cloud.base import CloudError, CloudErrorKind, GpuOffer, SshInfo

from cloud_gpu.chenyu import ChenyuProvider
from cloud_gpu.gpuhome import GpuhomeProvider

pytestmark = pytest.mark.anyio



def _chenyu(handler) -> ChenyuProvider:
    return ChenyuProvider({"api_key": "sk-test-1234"}, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("chenyu_code", "gpuhome_code", "expected"),
    [(1, 2, "开机中"), (2, 3, "运行中"), (3, 4, "关机中"), (4, 5, "已关机")],
)
def test_builtin_providers_use_same_lifecycle_copy(chenyu_code, gpuhome_code, expected):
    chenyu = ChenyuProvider._instance({"instance_uuid": "c", "status": chenyu_code})
    gpuhome = GpuhomeProvider._instance({"instance_uuid": "g", "status": gpuhome_code})
    assert chenyu.status == gpuhome.status
    assert chenyu.status_text == gpuhome.status_text == expected


def test_builtin_providers_use_same_launch_copy_without_losing_platform_limits():
    chenyu = {m["id"]: m for m in ChenyuProvider.describe()["launch_methods"]}
    gpuhome = {m["id"]: m for m in GpuhomeProvider.describe()["launch_methods"]}
    assert chenyu["startup"]["label"] == gpuhome["startup"]["label"] == "开启已关机实例"
    assert chenyu["image"]["label"] == gpuhome["image"]["label"] == "用私有镜像新建实例"
    assert chenyu["startup"]["configurable_gpu"]
    assert not gpuhome["startup"]["configurable_gpu"]
    assert gpuhome["image"]["help"] == "新建实例采用按量计费。"
    assert ChenyuProvider._instance({"status": 23}).status_text == "已关机（异常）"
    assert GpuhomeProvider._instance({"instance_uuid": "g", "status": 1}).status_text == (
        "开机中（排队中）"
    )


async def test_chenyu_lists_instances_and_maps_status() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "total": 3,
                    "instance_list": [
                        {
                            "instance_uuid": "a",
                            "status": 2,
                            "title": "5090",
                            "gpu_name": "RTX 5090",
                            "gpu_nums": 1,
                            "start_time": 1700000000,
                        },
                        {"instance_uuid": "b", "status": 22, "title": "old"},
                        {"instance_uuid": "c", "status": 24},
                    ],
                },
            },
        )

    async with _chenyu(handler) as provider:
        instances = await provider.list_instances()
    assert [(i.id, i.status, i.status_text) for i in instances] == [
        ("a", "running", "运行中"),
        ("b", "stopped", "已关机"),
        ("c", "starting", "开机中"),
    ]
    assert instances[0].gpu_name == "RTX 5090" and instances[0].started_at == 1700000000
    assert len(seen) == 1  # total 已满足，不再翻页
    assert seen[0].headers["Authorization"] == "Bearer sk-test-1234"
    assert seen[0].url.path == "/api/open/v2/instance/list"


async def test_chenyu_shutdown_and_idle_close_requests() -> None:
    bodies: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"code": 0, "msg": "ok"})

    async with _chenyu(handler) as provider:
        await provider.shutdown("abc")
        await provider.destroy("abc")
        await provider.set_idle_close("abc", 60)
        with pytest.raises(CloudError):
            await provider.set_idle_close("abc", 45)
    assert bodies == [
        ("/api/open/v2/instance/shutdown", {"instance_uuid": "abc"}),
        ("/api/open/v2/instance/destroy", {"instance_uuid": "abc"}),
        (
            "/api/open/v2/instance/set_idle_close",
            {"instance_uuid": "abc", "idle_period_minutes": 60, "destroy": False},
        ),
    ]


async def test_chenyu_save_image_custom_and_default_names() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/open/v2/instance/save_image"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0})

    async with _chenyu(handler) as provider:
        await provider.save_image("abc", "  干净环境  ")
        await provider.save_image("abc")
        await provider.save_image("abc", "   ")
    assert bodies == [
        {"instance_uuid": "abc", "image_name": "干净环境"},
        {"instance_uuid": "abc"},
        {"instance_uuid": "abc"},
    ]


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(401, text="no"), CloudErrorKind.AUTH),
        (httpx.Response(429, text="slow"), CloudErrorKind.RATE_LIMIT),
        (httpx.Response(502, text="bad gateway"), CloudErrorKind.TRANSIENT),
        (httpx.Response(200, json={"code": 1, "msg": "API Key 无效"}), CloudErrorKind.AUTH),
        (httpx.Response(200, json={"code": 1, "msg": "账户余额不足"}), CloudErrorKind.FATAL),
        (
            httpx.Response(
                200, json={"code": 10, "error_code": "GPU_CAPACITY_EXHAUSTED", "msg": "无卡"}
            ),
            CloudErrorKind.OUT_OF_STOCK,
        ),
        (httpx.Response(200, json={"code": 7, "msg": "系统繁忙"}), CloudErrorKind.TRANSIENT),
    ],
)
async def test_chenyu_classifies_errors(response: httpx.Response, kind: CloudErrorKind) -> None:
    async with _chenyu(lambda request: response) as provider:
        with pytest.raises(CloudError) as caught:
            await provider.shutdown("abc")
    assert caught.value.kind == kind


async def test_chenyu_requires_key() -> None:
    async with ChenyuProvider(
        {}, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as provider:
        with pytest.raises(CloudError) as caught:
            await provider.list_instances()
    assert caught.value.kind == CloudErrorKind.AUTH


async def test_chenyu_test_reports_balance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/balance/info"):
            return httpx.Response(
                200, json={"code": 0, "data": {"balance": 12.5, "card_balance": 0}}
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"total": 1, "instance_list": [{"instance_uuid": "a", "status": 2}]},
            },
        )

    async with _chenyu(handler) as provider:
        message = await provider.test()
    assert message == "连接成功：共 1 台实例，1 台运行中。充值余额 12.5，算力卡余额 0。"


async def test_chenyu_launch_requests_and_catalog() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/open/v2")
        body = json.loads(request.content) if request.content else dict(request.url.params)
        calls.append((path, body))
        data: dict = {}
        if path == "/gpu/list":
            data = {
                "gpu_list": [
                    {
                        "gpu_uuid": "g5090",
                        "gpu_name": "RTX 5090",
                        "status": 1,
                        "price": {"hour": 3.2},
                    }
                ]
            }
        elif path == "/image/private/list":
            data = {
                "image_list": [
                    {
                        "uuid": "img1",
                        "title": "worker",
                        "size": 30 * 1024**3,
                        "save_image_status": 2,
                    }
                ]
            }
        elif path == "/pod/list":
            data = {
                "pod_list": [
                    {"uuid": "pod1", "title": "PyTorch", "pod_tag": ["2.4 2.5"], "remark": ""}
                ]
            }
        elif path == "/instance/list":
            data = {
                "total": 2,
                "instance_list": [
                    {"instance_uuid": "off", "status": 22, "title": "旧机", "gpu_name": "RTX 4090"},
                    {
                        "instance_uuid": "on",
                        "status": 2,
                        "gpu_uuid": "g5090",
                        "server_map": [
                            {"port_type": "http", "url": "x"},
                            {
                                "port_type": "ssh",
                                "ssh_info": {
                                    "host": "h.example",
                                    "port": 2201,
                                    "username": "root",
                                    "password": "pw",
                                },
                            },
                        ],
                    },
                ],
            }
        elif path == "/instance/create_by_image":
            data = {"instance_uuid": "new1"}
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": data})

    async with _chenyu(handler) as provider:
        assert await provider.gpu_offers() == [
            GpuOffer(id="g5090", label="RTX 5090", available=True, price_per_hour=3.2)
        ]
        assert [t.id for t in await provider.launch_targets("startup")] == ["off"]
        images = await provider.launch_targets("image")
        assert images[0].detail == "30.0 GB"
        pods = await provider.launch_targets("pod", "torch")
        assert [t.id for t in pods] == ["pod1|2.4", "pod1|2.5"]
        assert await provider.launch("startup", "off", "g4090", 1) == "off"
        assert await provider.launch("image", "img1", "g5090", 2) == "new1"
        assert await provider.launch("pod", "pod1|2.5", "g5090", 1) is None
        running = next(i for i in await provider.list_instances() if i.id == "on")
    assert running.ssh == SshInfo(host="h.example", port=2201, username="root", password="pw")
    assert running.gpu_id == "g5090"
    assert "ssh" not in running.model_dump()  # 密码不出现在接口输出中
    assert ("/pod/list", {"page": "1", "page_size": "30", "name": "torch"}) in calls
    assert (
        "/instance/startup",
        {"instance_uuid": "off", "gpu_uuid": "g4090", "gpu_nums": 1},
    ) in calls
    assert (
        "/instance/create_by_image",
        {"image_uuid": "img1", "gpu_uuid": "g5090", "gpu_nums": 2},
    ) in calls
    assert (
        "/instance/create_by_pod",
        {"pod_uuid": "pod1", "pod_tag": "2.5", "gpu_uuid": "g5090", "gpu_nums": 1},
    ) in calls
