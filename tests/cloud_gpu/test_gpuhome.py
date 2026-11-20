import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import prepare_server
from fastapi.testclient import TestClient
from video_turbo.cloud.base import CAP_IDLE_CLOSE, CloudError, CloudErrorKind
from video_turbo.cloud_store import CloudBindingRequest, CloudSettings
from video_turbo.config import AppConfig
from video_turbo.database import Database
from video_turbo.models import WorkerRegistration
from video_turbo.orchestrator.app import app
from video_turbo.orchestrator.cloud_service import CloudService

from cloud_gpu.gpuhome import GpuhomeProvider

pytestmark = pytest.mark.anyio

TOKEN = "test-session-token"
PHONE = "13000000000"


def ok(data=None):
    return httpx.Response(200, json={"code": 0, "data": data})


def instance(**fields):
    return {
        "instance_uuid": "test-instance",
        "instance_id": 123,
        "nick_name": "测试实例",
        "status": 3,
        "product_name": "RTX 3090",
        "req_gpu_count": 2,
        "datacenter_id": 2,
        "datacenter_name": "四川",
        "ssh_command": "ssh root@connect.gpuhome.cc -p 12345",
        "ssh_password": "ssh-secret",
        **fields,
    }


def provider(handler, credentials=None):
    return GpuhomeProvider(
        credentials if credentials is not None else {"phone": PHONE, "token": TOKEN},
        transport=httpx.MockTransport(handler),
    )


async def test_login_exchanges_password_and_saved_token_survives_new_client():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("password_login"):
            assert json.loads(request.content) == {"phone": PHONE, "pass_word": " password "}
            assert "authorization" not in request.headers
            return ok({"token": TOKEN})
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return ok({"dataList": [instance()], "totalRecord": 1})

    async with provider(handler, {"phone": PHONE, "password": " password "}) as client:
        authorized = await client.prepare_credentials()
    assert authorized == {"phone": PHONE, "token": TOKEN}
    async with provider(handler, authorized) as client:
        items = await client.list_instances()
    assert calls.count("/api/v1/user/password_login") == 1
    assert items[0].status == "running" and items[0].gpu_count == 2
    assert items[0].gpu_id == "RTX 3090|2"
    assert items[0].region == "四川"
    assert items[0].ssh.host == "connect.gpuhome.cc" and items[0].ssh.port == 12345
    assert "ssh" not in items[0].model_dump() and "ssh-secret" not in str(items[0].model_dump())


async def test_credentials_reject_client_tokens_and_changed_account_without_password():
    saved = {"phone": PHONE, "token": TOKEN}
    assert GpuhomeProvider.credentials_configured(saved)
    assert GpuhomeProvider.merge_credentials(saved, {}) == saved
    assert GpuhomeProvider.merge_credentials(saved, {"password": " new "}) == {
        "phone": PHONE,
        "password": " new ",
    }
    for values in ({"phone": "13900000000"}, {"token": "injected", "phone": PHONE}):
        with pytest.raises(CloudError):
            GpuhomeProvider.merge_credentials({}, values)
    with pytest.raises(CloudError):
        GpuhomeProvider.merge_credentials(saved, {"phone": "13900000000"})
    assert CAP_IDLE_CLOSE not in GpuhomeProvider.capabilities


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        (
            {"account_remain_amount": "12.34", "voucher_remain_amount": 5},
            "账户总余额 ¥17.34，现金余额 ¥12.34，代金券余额 ¥5.00。",
        ),
        (
            {"account_remain_amount": 0, "voucher_remain_amount": "0"},
            "账户总余额 ¥0.00，现金余额 ¥0.00，代金券余额 ¥0.00。",
        ),
        (
            {"account_remain_amount": "-1.20", "voucher_remain_amount": 0},
            "账户总余额 ¥-1.20，现金余额 ¥-1.20，代金券余额 ¥0.00。",
        ),
        ({"voucher_remain_amount": "2.50"}, "代金券余额 ¥2.50。"),
        (
            {"account_remain_amount": "NaN", "voucher_remain_amount": "Infinity"},
            "余额暂不可用，请稍后重试。",
        ),
        (
            {"account_remain_amount": "bad", "voucher_remain_amount": True},
            "余额暂不可用，请稍后重试。",
        ),
        ({}, "余额暂不可用，请稍后重试。"),
    ],
)
async def test_connection_reports_balance_in_yuan(account, expected):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path.endswith("/user/user_info"):
            return ok(account)
        return ok({"dataList": [instance()], "totalRecord": 1})

    async with provider(handler) as client:
        assert await client.test() == "连接成功：共 1 台实例，1 台运行中。" + expected
    assert calls == ["/api/v1/jupyter/list_notebook", "/api/v1/user/user_info"]


@pytest.mark.parametrize("status", [403, 503])
async def test_balance_failure_does_not_report_zero_or_hide_auth_error(status):
    def handler(request):
        if request.url.path.endswith("/user/user_info"):
            return httpx.Response(status)
        return ok({"dataList": [], "totalRecord": 0})

    async with provider(handler) as client:
        if status == 403:
            with pytest.raises(CloudError) as caught:
                await client.test()
            assert caught.value.kind == CloudErrorKind.AUTH
        else:
            assert (
                await client.test()
                == "连接成功：共 0 台实例，0 台运行中。余额暂不可用，请稍后重试。"
            )


async def test_pagination_statuses_and_deleted_instances():
    pages = []

    def handler(request):
        page = int(request.url.params["page_no"])
        pages.append(page)
        rows = (
            [instance(instance_uuid="a", status="5")]
            if page == 1
            else [
                instance(instance_uuid="b", status=14),
                instance(instance_uuid="deleted", status=8),
                instance(instance_uuid="error", status=7),
            ]
        )
        return ok({"dataList": rows, "totalRecord": 4})

    async with provider(handler) as client:
        items = await client.list_instances()
    assert pages == [1, 2]
    assert [(i.id, i.status, i.status_text) for i in items] == [
        ("a", "stopped", "已关机"),
        ("b", "starting", "开机中（镜像拉取中）"),
        ("error", "unknown", "运行异常"),
    ]


async def test_incomplete_pagination_is_error_not_missing_instances():
    async with provider(lambda r: ok({"dataList": [instance()], "totalRecord": 2})) as client:
        with pytest.raises(CloudError, match="分页不完整"):
            await client.list_instances()


@pytest.mark.parametrize("command", ["", "not-ssh root@host", "ssh root@host -p nope"])
async def test_invalid_ssh_does_not_break_instance_list(command):
    result = GpuhomeProvider._instance(instance(ssh_command=command))
    assert result.ssh is None


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(401), CloudErrorKind.AUTH),
        (httpx.Response(403), CloudErrorKind.AUTH),
        (httpx.Response(429), CloudErrorKind.RATE_LIMIT),
        (httpx.Response(503), CloudErrorKind.TRANSIENT),
        (
            httpx.Response(200, json={"code": "B2006", "msg": "token 信息验证失败"}),
            CloudErrorKind.AUTH,
        ),
        (httpx.Response(200, json={"code": 10010, "message": "失效"}), CloudErrorKind.AUTH),
        (
            httpx.Response(200, json={"code": 10003, "message": "资源不足"}),
            CloudErrorKind.OUT_OF_STOCK,
        ),
        (httpx.Response(200, json={"code": 1, "message": "账户余额不足"}), CloudErrorKind.FATAL),
        (httpx.Response(200, json={"code": 1, "message": "系统繁忙"}), CloudErrorKind.TRANSIENT),
        (httpx.Response(200, text="<html>登录</html>"), CloudErrorKind.TRANSIENT),
    ],
)
async def test_error_classification_does_not_relogin_or_retry_mutations(response, kind):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return response

    async with provider(handler) as client:
        with pytest.raises(CloudError) as error:
            await client.shutdown("test-instance")
    assert error.value.kind == kind
    assert calls == ["/api/v1/order/stop"]
    assert TOKEN not in str(error.value)


async def test_resource_actions_use_correct_ids_and_payloads():
    sent = []

    def handler(request):
        if request.method == "GET":
            return ok({"dataList": [instance(status=5)], "totalRecord": 1})
        sent.append((request.url.path, json.loads(request.content)))
        return ok()

    async with provider(handler) as client:
        assert await client.launch("startup", "test-instance", "existing", 1) == "test-instance"
        await client.shutdown("test-instance")
        await client.destroy("test-instance")
        await client.save_image("test-instance", " 环境 ")
        with pytest.raises(CloudError, match="最多 10"):
            await client.save_image("test-instance", "x" * 11)
        with pytest.raises(CloudError):
            await client.set_idle_close("test-instance", 60)
    assert sent == [
        ("/api/v1/order/start", {"webide_instance_uuid": "test-instance"}),
        ("/api/v1/order/stop", {"webide_instance_uuid": "test-instance"}),
        ("/api/v1/order/release", {"webide_instance_uuid": "test-instance"}),
        ("/api/v1/images/self/save", {"webide_run_id": 123, "image_name": "环境"}),
    ]


async def test_gpu_prices_preserve_region_count_totals_and_stock():
    products = [
        {
            "gpu_name": "RTX 3090",
            "gpu_memory": 24 * 1024**3,
            "sys_disk_size": 30 * 1024**3,
            "data_disk_size": 50 * 1024**3,
            "product_skus": [
                {
                    "gpu_sku_code": "eight",
                    "datacenter_id": 2,
                    "gpu_num": 8,
                    "current_price": 792,
                    "nodes_left": 0,
                },
                {
                    "gpu_sku_code": "one",
                    "datacenter_id": 2,
                    "gpu_num": 1,
                    "current_price": "99",
                    "nodes_left": 1,
                    "cpu_per_gpu": 20,
                    "mem_per_gpu": 100 * 1024**3,
                },
                {
                    "gpu_sku_code": "two",
                    "datacenter_id": 2,
                    "gpu_num": 2,
                    "current_price": 198,
                    "nodes_left": 0,
                },
                {
                    "gpu_sku_code": "other-region",
                    "datacenter_id": 14,
                    "gpu_num": 2,
                    "current_price": 176,
                    "nodes_left": 3,
                },
            ],
        },
        {
            "gpu_name": "CPU",
            "gpu_memory": 0,
            "product_skus": [{"gpu_sku_code": "cpu", "datacenter_id": 2, "gpu_num": 1}],
        },
    ]

    def handler(request):
        if request.url.path.endswith("/data_center/tag"):
            return ok(
                [
                    {"data_center_id": 2, "data_center_name": "四川"},
                    {"data_center_id": 14, "data_center_name": "杭州"},
                ]
            )
        return ok(products)

    async with provider(handler) as client:
        offers = await client.gpu_offers()
    assert [g.label for g in offers] == ["RTX 3090 · 四川", "RTX 3090 · 杭州"]
    assert offers[0].price_per_hour == 0.99
    assert [(c.gpu_count, c.price_per_hour, c.available) for c in offers[0].configurations] == [
        (1, 0.99, True),
        (2, 1.98, False),
        (8, 7.92, False),
    ]
    assert offers[1].price_per_hour == 0.88  # 2 卡 1.76 元，通用单卡价 0.88 元。
    assert offers[1].configurations[0].price_per_hour == 1.76
    assert "20 核 CPU" in offers[0].configurations[0].detail
    assert "100 GB 内存" in offers[0].configurations[0].detail
    assert "30 GB 系统盘" in offers[0].configurations[0].detail


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (None, None),
        ("bad", None),
        ("NaN", None),
        ("Infinity", None),
        (-1, None),
        (True, None),
        (0, 0.0),
    ],
)
async def test_unknown_prices_and_stock_are_not_reported_as_free_or_sold_out(price, expected):
    config = GpuhomeProvider._configuration({}, {"gpu_num": 2, "current_price": price})
    assert config.price_per_hour == expected and config.available is None


async def test_duplicate_skus_choose_the_same_in_stock_configuration_for_display_and_create():
    sent = []
    products = [
        {
            "gpu_name": "RTX 4090",
            "gpu_memory": 24,
            "product_skus": [
                {
                    "gpu_sku_code": "old",
                    "datacenter_id": 2,
                    "gpu_num": 1,
                    "current_price": 100,
                    "nodes_left": 0,
                },
                {
                    "gpu_sku_code": "current",
                    "datacenter_id": 2,
                    "gpu_num": 1,
                    "current_price": 120,
                    "nodes_left": 1,
                },
            ],
        }
    ]

    def handler(request):
        if request.url.path.endswith("/products/list"):
            return ok(products)
        if request.url.path.endswith("/data_center/tag"):
            return ok([])
        if request.url.path.endswith("/images/self/list"):
            return ok([{"image_id": 1, "image_status": 2}])
        sent.append(json.loads(request.content))
        return ok({"instance_uuid": "new"})

    async with provider(handler) as client:
        assert (await client.gpu_offers())[0].configurations[0].price_per_hour == 1.2
        assert await client.launch("image", "1", "RTX 4090|2", 1) == "new"
    assert sent[0]["product_sku_code"] == "current"


async def test_image_choices_include_size_version_and_region():
    def handler(request):
        if request.url.path.endswith("/images/self/list"):
            return ok(
                [
                    {
                        "image_id": 1,
                        "image_status": 2,
                        "image_name": "环境",
                        "image_size": 30 * 1024**3,
                        "base_image_version": "CUDA12.8",
                        "cache_data_center": "四川",
                    },
                    {"image_id": 2, "image_status": 1, "image_name": "保存中"},
                ]
            )
        return ok(
            [
                {
                    "display_name": "PyTorch",
                    "display_versions": [
                        {"image_id": 3, "version": "v2", "size": "15G", "notes": "Ubuntu22"},
                    ],
                }
            ]
        )

    async with provider(handler) as client:
        private = await client.launch_targets("image")
        public = await client.launch_targets("public")
    assert len(private) == 1
    assert private[0].detail == "30.0 GB · CUDA12.8 · 地区：四川"
    assert public[0].detail == "15G · Ubuntu22"


def test_launch_options_api_exposes_configuration_prices_without_password(
    tmp_path: Path, monkeypatch
):
    db = prepare_server(tmp_path, monkeypatch)
    db.set_cloud_profile("gpuhome", {"type": "gpuhome"})
    db.set_cloud_credentials("gpuhome", {"phone": PHONE, "token": TOKEN})

    def handler(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path.endswith("/data_center/tag"):
            return ok([{"data_center_id": 2, "data_center_name": "四川"}])
        return ok(
            [
                {
                    "gpu_name": "RTX 3090",
                    "gpu_memory": 24,
                    "product_skus": [
                        {
                            "gpu_sku_code": "two",
                            "gpu_num": 2,
                            "datacenter_id": 2,
                            "current_price": 198,
                            "nodes_left": 0,
                        },
                    ],
                }
            ]
        )

    with TestClient(app) as client:
        app.state.cloud.transport = httpx.MockTransport(handler)
        response = client.get("/api/cloud/providers/gpuhome/launch-options")
    assert response.status_code == 200
    offer = response.json()["gpus"][0]
    assert offer["price_per_hour"] == 0.99
    assert offer["configurations"] == [
        {"gpu_count": 2, "price_per_hour": 1.98, "available": False, "detail": ""},
    ]
    assert TOKEN not in response.text and "password" not in response.text


@pytest.mark.parametrize(
    ("method", "target"), [("image", "1"), ("public", "2"), ("community", "3")]
)
async def test_image_create_matches_gpu_region_and_count(method, target):
    sent = []

    def handler(request):
        path = request.url.path
        if path.endswith("/data_center/tag"):
            return ok([{"data_center_id": 2, "data_center_name": "四川"}])
        if path.endswith("/products/list"):
            return ok(
                [
                    {
                        "gpu_name": "RTX 4090",
                        "gpu_memory": 24,
                        "product_skus": [
                            {
                                "gpu_sku_code": "sku-one",
                                "datacenter_id": 2,
                                "gpu_num": 1,
                                "nodes_left": 0,
                            },
                            {
                                "gpu_sku_code": "sku-two",
                                "datacenter_id": 2,
                                "gpu_num": 2,
                                "nodes_left": 1,
                            },
                        ],
                    }
                ]
            )
        if path.endswith("/self/list"):
            return ok([{"image_id": 1, "image_name": "私有环境", "image_status": 2}])
        if path.endswith("/public/display_list"):
            return ok(
                [
                    {
                        "display_name": "PyTorch",
                        "display_versions": [{"image_id": 2, "version": "v1"}],
                    }
                ]
            )
        if path.endswith("/community/all"):
            return ok([{"image_id": 3, "image_name": "ComfyUI"}])
        sent.append(json.loads(request.content))
        return ok({"instance_uuid": "new-instance"})

    async with provider(handler) as client:
        offers = await client.gpu_offers()
        assert len(offers) == 1 and offers[0].available
        assert offers[0].label == "RTX 4090 · 四川"
        assert await client.launch(method, target, "RTX 4090|2", 2) == "new-instance"
        with pytest.raises(CloudError, match="不支持所选 GPU 数量"):
            await client.launch(method, target, "RTX 4090|2", 8)
        assert len(await client.launch_targets("community", "comfy")) == 1
        assert not await client.launch_targets("community", "does-not-exist")
    assert sent == [
        {
            "product_sku_code": "sku-two",
            "pay_type": "usetime",
            "gpu_num": 2,
            "image_id": 0 if method == "image" else int(target),
            "self_image_id": int(target) if method == "image" else 0,
        }
    ]


def test_login_api_persists_only_token_and_preserves_old_authorization_on_failure(
    tmp_path: Path, monkeypatch
):
    db = prepare_server(tmp_path, monkeypatch)
    db.set_cloud_profile("gpuhome", {"type": "gpuhome"})
    logins = []

    def handler(request):
        if request.url.path.endswith("password_login"):
            payload = json.loads(request.content)
            logins.append(payload)
            if payload["pass_word"] == "wrong":
                return httpx.Response(200, json={"code": 1, "message": "密码错误"})
            return ok({"token": TOKEN})
        return ok({"dataList": [instance()], "totalRecord": 1})

    with TestClient(app) as client:
        app.state.cloud.transport = httpx.MockTransport(handler)
        url = "/api/cloud/providers/gpuhome"
        payload = {"values": {"phone": PHONE, "password": " password "}}
        assert client.post(url + "/test", json=payload).status_code == 200
        assert db.cloud_credentials("gpuhome") == {}
        assert client.put(url + "/credentials", json=payload).json() == {"saved": True}
        saved = {"phone": PHONE, "token": TOKEN}
        assert db.cloud_credentials("gpuhome") == saved
        # 重建客户端和数据库读取仍只使用 Token；不要求用户重填密码。
        assert client.get("/api/cloud/instances").json()[0]["instances"][0]["id"] == "test-instance"
        listing = client.get("/api/cloud/providers").json()
        info = next(p for p in listing if p["id"] == "gpuhome")
        assert info["configured"] and info["credential_mode"] == "login"
        assert info["values"] == {"phone": PHONE, "password": ""}
        assert TOKEN not in json.dumps(listing) and " password " not in json.dumps(listing)
        assert client.put(url + "/credentials", json={"values": {}}).status_code == 200
        assert len(logins) == 2  # 测试连接和登录保存各一次；已有 Token 不重新登录。
        assert (
            client.put(url + "/credentials", json={"values": {"password": "wrong"}}).status_code
            == 400
        )
        assert db.cloud_credentials("gpuhome") == saved
        assert client.delete(url + "/credentials").status_code == 200
        assert db.cloud_credentials("gpuhome") == {}
    assert b" password " not in db.path.read_bytes()


def _ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


async def test_gpuhome_auto_shutdown_without_platform_idle_backstop(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    db.initialize()
    db.set_cloud_settings(CloudSettings(idle_minutes=10, backstop_minutes=45))
    service = CloudService(AppConfig(), db, SimpleNamespace(paused=False, scan_status="idle"))
    for name in ("cloud5090-01", "cloud5090-02"):
        db.register_worker(
            WorkerRegistration(
                name=name,
                version="0.0.0",
                gpu_name="RTX 5090",
                memory_total_mb=32000,
                model="large-v3",
                compute_type="float16",
                supported_asr_modes=["whisperjav", "faster_whisper"],
                supports_whisperjav_ensemble=True,
            )
        )
    db.set_cloud_profile("gpuhome", {"type": "gpuhome"})
    db.set_cloud_credentials("gpuhome", {"phone": "13000000000", "token": "test-token"})
    db.bind_cloud_instance(
        "cloud5090",
        CloudBindingRequest(provider="gpuhome", instance_id="i-1", shutdown_mode="always"),
    )
    calls = []
    status = 3

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v1/jupyter/list_notebook":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "dataList": [{"instance_uuid": "i-1", "status": status}],
                        "totalRecord": 1,
                    },
                },
            )
        assert request.url.path == "/api/v1/order/stop"
        assert json.loads(request.content) == {"webide_instance_uuid": "i-1"}
        return httpx.Response(200, json={"code": 0})

    service.transport = httpx.MockTransport(handler)
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert binding["idle_since"] and binding["backstop_minutes"] is None
    db.update_cloud_binding("cloud5090", idle_since=_ago(minutes=11))
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["state"] == "draining"
    db.update_cloud_binding("cloud5090", drain_started_at=_ago(seconds=61))
    await service.process_once()
    assert db.get_cloud_binding("cloud5090")["state"] == "stopping"
    assert calls.count(("POST", "/api/v1/order/stop")) == 1
    status = 5
    await service.process_once()
    binding = db.get_cloud_binding("cloud5090")
    assert binding["state"] == "stopped" and binding["shutdown_mode"] == "always"
