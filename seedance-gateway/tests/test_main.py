import hashlib
import hmac
import os
import tempfile

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import main
from models import TaskStatus, TaskStatusResponse


def build_expected_status_token(task_id: str, expires_at: int, secret: str = "secret-token") -> str:
    payload = f"{task_id}:{expires_at}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def build_expected_status_url(base_url: str, task_id: str, expires_at: int, secret: str = "secret-token") -> str:
    return (
        f"{base_url}/v1/tasks/{task_id}?expires_at={expires_at}"
        f"&status_token={build_expected_status_token(task_id, expires_at, secret)}"
    )


class FakeSeedanceClient:
    def __init__(self, task_id: str = "task-123", error: str | None = None):
        self.task_id = task_id
        self.error = error
        self.requests = []

    async def submit_task(self, request):
        self.requests.append(request)
        return self.task_id, self.error


class FakeTaskManager:
    def __init__(self, task_responses: dict[str, TaskStatusResponse] | None = None, is_healthy: bool = True):
        self.created = []
        self.task_responses = task_responses or {}
        self.is_healthy = is_healthy

    async def create_task(self, task_id: str, prompt: str | None, provider_slug: str | None = None):
        self.created.append((task_id, prompt))

    async def get_task(self, task_id: str, provider_slug: str | None = None) -> TaskStatusResponse:
        return self.task_responses.get(
            task_id,
            TaskStatusResponse(id=task_id, status=TaskStatus.FAILED, error="Task not found"),
        )

    async def health_check(self) -> bool:
        return self.is_healthy


@pytest.mark.asyncio
async def test_create_video_returns_openai_style_response(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    monkeypatch.delenv("GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.setattr(main.time, "time", lambda: 1700000000)
    fake_client = FakeSeedanceClient(task_id="seed-task-1")
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "seedance_client", fake_client)
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/video/generations",
            headers={"Authorization": "Bearer secret-token"},
            json={"prompt": "generate a short clip", "model": "seedance-pro"},
        )

    expected_status_url = build_expected_status_url("http://localhost:8001", "seed-task-1", 1700000360)
    assert response.status_code == 202
    assert response.json() == {
        "object": "list",
        "data": [{"url": expected_status_url}],
        "model": "seedance-pro",
        "id": "seed-task-1",
    }
    assert len(fake_client.requests) == 1
    assert fake_manager.created == [("seed-task-1", "generate a short clip")]


@pytest.mark.asyncio
async def test_create_chat_completion_returns_task_summary(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    monkeypatch.delenv("GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.setattr(main.time, "time", lambda: 1700000000)
    fake_client = FakeSeedanceClient(task_id="seed-task-chat-1")
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "seedance_client", fake_client)
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "model": "seedance-2.0-fast",
                "messages": [
                    {"role": "system", "content": "You are a helper."},
                    {"role": "user", "content": "generate a short clip"},
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    expected_status_url = build_expected_status_url("http://localhost:8001", "seed-task-chat-1", 1700000360)
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "seedance-2.0-fast"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert "seed-task-chat-1" in payload["choices"][0]["message"]["content"]
    assert expected_status_url in payload["choices"][0]["message"]["content"]
    assert fake_client.requests[0].prompt == "generate a short clip"
    assert fake_client.requests[0].fast is True
    assert fake_manager.created == [("seed-task-chat-1", "generate a short clip")]


@pytest.mark.asyncio
async def test_create_chat_completion_extracts_text_from_content_parts(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    fake_client = FakeSeedanceClient(task_id="seed-task-chat-2")
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "seedance_client", fake_client)
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "model": "seedance-2.0",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "first line"},
                            {"type": "text", "text": "second line"},
                        ],
                    }
                ],
                "duration": 5,
            },
        )

    assert response.status_code == 200
    assert fake_client.requests[0].prompt == "first line\nsecond line"
    assert fake_client.requests[0].fast is False
    assert fake_client.requests[0].duration == 5


@pytest.mark.asyncio
async def test_create_chat_completion_rejects_missing_user_prompt(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "model": "seedance-2.0-fast",
                "messages": [{"role": "system", "content": "Only instructions."}],
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "A user message is required to create a video task"}


@pytest.mark.asyncio
async def test_create_chat_completion_alias_on_v1_returns_task_summary(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    monkeypatch.delenv("GATEWAY_PUBLIC_URL", raising=False)
    fake_client = FakeSeedanceClient(task_id="seed-task-chat-3")
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "seedance_client", fake_client)
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "model": "seedance-2.0-fast",
                "messages": [{"role": "user", "content": "generate through alias"}],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert "seed-task-chat-3" in payload["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_create_chat_completion_alias_on_v1_rejects_missing_user_prompt(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "model": "seedance-2.0-fast",
                "messages": [{"role": "assistant", "content": "missing user prompt"}],
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "A user message is required to create a video task"}


@pytest.mark.asyncio
async def test_get_task_status_rejects_unsigned_request(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    fake_manager = FakeTaskManager(
        task_responses={
            "seed-task-status-1": TaskStatusResponse(
                id="seed-task-status-1",
                status=TaskStatus.PROCESSING,
                prompt="track this task",
                progress=42,
            )
        }
    )
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/tasks/seed-task-status-1")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid token"}


@pytest.mark.asyncio
async def test_get_task_status_allows_signed_url_access(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    expires_at = 4102444800
    fake_manager = FakeTaskManager(
        task_responses={
            "seed-task-status-1": TaskStatusResponse(
                id="seed-task-status-1",
                status=TaskStatus.PROCESSING,
                prompt="track this task",
                progress=42,
            )
        }
    )
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/tasks/seed-task-status-1",
            params={
                "expires_at": expires_at,
                "status_token": build_expected_status_token("seed-task-status-1", expires_at),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "id": "seed-task-status-1",
        "status": "processing",
        "prompt": "track this task",
        "result_url": None,
        "error": None,
        "progress": 42,
        "created_at": None,
    }


@pytest.mark.asyncio
async def test_get_task_status_keeps_not_found_payload_with_signed_url(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    expires_at = 4102444800
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/tasks/missing-task",
            params={
                "expires_at": expires_at,
                "status_token": build_expected_status_token("missing-task", expires_at),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "id": "missing-task",
        "status": "failed",
        "prompt": None,
        "result_url": None,
        "error": "Task not found",
        "progress": 0,
        "created_at": None,
    }


@pytest.mark.asyncio
async def test_get_task_status_rejects_expired_signed_url(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    fake_manager = FakeTaskManager(
        task_responses={
            "seed-task-status-expired": TaskStatusResponse(
                id="seed-task-status-expired",
                status=TaskStatus.PROCESSING,
            )
        }
    )
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/tasks/seed-task-status-expired",
            params={
                "expires_at": 1700000000,
                "status_token": build_expected_status_token("seed-task-status-expired", 1700000000),
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid token"}


@pytest.mark.asyncio
async def test_create_video_forwards_ppio_request_fields(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    fake_client = FakeSeedanceClient(task_id="seed-task-3")
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "seedance_client", fake_client)
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/video/generations",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "prompt": "generate a short clip",
                "model": "seedance-2.0",
                "fast": True,
                "duration": 6,
                "resolution": "720p",
                "reference_images": ["https://cdn.example/ref-1.png"],
                "return_last_frame": True,
            },
        )

    assert response.status_code == 202
    upstream_request = fake_client.requests[0]
    assert upstream_request.prompt == "generate a short clip"
    assert upstream_request.fast is True
    assert upstream_request.duration == 6
    assert upstream_request.resolution == "720p"
    assert upstream_request.reference_images == ["https://cdn.example/ref-1.png"]
    assert upstream_request.return_last_frame is True


@pytest.mark.asyncio
async def test_create_video_allows_image_only_request(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    fake_client = FakeSeedanceClient(task_id="seed-task-4")
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "seedance_client", fake_client)
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/video/generations",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "image": "https://cdn.example/first-frame.png",
                "duration": 5,
                "resolution": "480p",
            },
        )

    assert response.status_code == 202
    upstream_request = fake_client.requests[0]
    assert upstream_request.prompt is None
    assert upstream_request.image == "https://cdn.example/first-frame.png"
    assert fake_manager.created == [("seed-task-4", None)]


@pytest.mark.asyncio
async def test_create_video_uses_gateway_public_url_when_configured(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    monkeypatch.setenv("GATEWAY_PUBLIC_URL", "https://gateway.example")
    monkeypatch.setattr(main.time, "time", lambda: 1700000000)
    fake_client = FakeSeedanceClient(task_id="seed-task-2")
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "seedance_client", fake_client)
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/video/generations",
            headers={"Authorization": "Bearer secret-token"},
            json={"prompt": "generate a short clip", "model": "seedance-pro"},
        )

    assert response.status_code == 202
    assert response.json()["data"] == [{"url": build_expected_status_url("https://gateway.example", "seed-task-2", 1700000360)}]


@pytest.mark.asyncio
async def test_create_video_rejects_invalid_token(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/video/generations",
            headers={"Authorization": "Bearer wrong-token"},
            json={"prompt": "generate a short clip", "model": "seedance-pro"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid token"}


@pytest.mark.asyncio
async def test_create_video_rejects_missing_upstream_task_id(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")
    fake_client = FakeSeedanceClient(task_id=None)
    fake_manager = FakeTaskManager()
    monkeypatch.setattr(main, "seedance_client", fake_client)
    monkeypatch.setattr(main, "task_manager", fake_manager)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/video/generations",
            headers={"Authorization": "Bearer secret-token"},
            json={"prompt": "generate a short clip", "model": "seedance-pro"},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Upstream error: Missing task id from upstream response"}
    assert fake_manager.created == []


def test_verify_token_raises_server_error_when_gateway_token_missing(monkeypatch):
    monkeypatch.delenv("GATEWAY_ACCESS_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        main.verify_token("Bearer any-token")

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Gateway access token is not configured"


def test_load_runtime_config_rejects_missing_required_env(monkeypatch):
    for env_name in (
        "SEEDANCE_API_KEYS",
        "SEEDANCE_BASE_URL",
        "REDIS_URL",
        "GATEWAY_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(env_name, raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        main.load_runtime_config()

    assert str(exc_info.value) == (
        "Missing required environment variables: SEEDANCE_API_KEYS, "
        "SEEDANCE_BASE_URL, REDIS_URL, GATEWAY_ACCESS_TOKEN"
    )


@pytest.mark.asyncio
async def test_healthz_returns_ok():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_returns_ready_when_dependencies_are_available(monkeypatch):
    monkeypatch.setattr(main, "seedance_client", FakeSeedanceClient())
    monkeypatch.setattr(main, "task_manager", FakeTaskManager(is_healthy=True))

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_readyz_returns_503_when_redis_is_unavailable(monkeypatch):
    monkeypatch.setattr(main, "seedance_client", FakeSeedanceClient())
    monkeypatch.setattr(main, "task_manager", FakeTaskManager(is_healthy=False))

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"detail": "Redis unavailable"}


@pytest.mark.asyncio
async def test_admin_page_returns_management_ui():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert "Provider Manager" in response.text
    assert "base_url" in response.text


@pytest.mark.asyncio
async def test_admin_page_exposes_project_tools_menu_and_core_actions():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert "项目功能菜单" in response.text
    assert "健康检查" in response.text
    assert "就绪检查" in response.text
    assert "复制 Chat 接口" in response.text
    assert "复制 Video 接口" in response.text
    assert "复制本地启动命令" in response.text
    assert "复制 Docker 部署命令" in response.text
    assert "密钥管理" in response.text
    assert "查看已有 API Keys" in response.text
    assert "保存 API Keys" in response.text
    assert "管理后台 Bearer Token" in response.text


def test_readme_admin_documents_project_tools_shortcuts():
    readme_path = os.path.join(os.path.dirname(main.__file__), "README_ADMIN.md")
    with open(readme_path, "r", encoding="utf-8") as readme_file:
        content = readme_file.read()

    assert "项目功能菜单" in content
    assert "健康检查" in content
    assert "就绪检查" in content
    assert "复制 Chat 接口" in content
    assert "复制 Docker 部署命令" in content
    assert "密钥管理" in content
    assert "查看已有 API Keys" in content
    assert "ADMIN_ACCESS_TOKEN" in content


@pytest.mark.asyncio
async def test_admin_page_reads_template_independent_of_current_working_directory(monkeypatch):
    original_cwd = os.getcwd()
    monkeypatch.chdir(tempfile.mkdtemp(prefix="seedance-admin-cwd-"))

    transport = ASGITransport(app=main.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/admin")
    finally:
        os.chdir(original_cwd)

    assert response.status_code == 200
    assert "Provider Manager" in response.text


@pytest.mark.asyncio
async def test_provider_management_api_can_create_and_list_provider(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        create_response = await client.post(
            "/admin/api/providers",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "name": "Demo Provider",
                "slug": "demo-provider",
                "base_url": "https://demo.example.com",
                "api_keys": ["sk-demo-1", "sk-demo-2"],
                "enabled": True,
                "is_default": True,
            },
        )
        list_response = await client.get(
            "/admin/api/providers",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert create_response.status_code == 201
    created_payload = create_response.json()
    assert created_payload["slug"] == "demo-provider"
    assert created_payload["name"] == "Demo Provider"
    assert created_payload["base_url"] == "https://demo.example.com"
    assert created_payload["api_key_count"] == 2
    assert created_payload["is_default"] is True

    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert len(list_payload["items"]) == 1
    assert list_payload["items"][0]["slug"] == "demo-provider"
    assert list_payload["items"][0]["video_generation_url"].endswith("/v1/providers/demo-provider/video/generations")


@pytest.mark.asyncio
async def test_provider_management_api_supports_dedicated_admin_token(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "gateway-token")
    monkeypatch.setenv("ADMIN_ACCESS_TOKEN", "admin-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        unauthorized_response = await client.get(
            "/admin/api/providers",
            headers={"Authorization": "Bearer gateway-token"},
        )
        authorized_response = await client.get(
            "/admin/api/providers",
            headers={"Authorization": "Bearer admin-token"},
        )

    assert unauthorized_response.status_code == 401
    assert authorized_response.status_code == 200
    assert authorized_response.json() == {"items": []}


@pytest.mark.asyncio
async def test_admin_routes_disable_caching(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        page_response = await client.get("/admin")
        api_response = await client.get(
            "/admin/api/providers",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert page_response.headers["cache-control"] == "no-store, private"
    assert page_response.headers["pragma"] == "no-cache"
    assert api_response.headers["cache-control"] == "no-store, private"
    assert api_response.headers["pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_provider_management_api_can_update_set_default_and_delete_provider(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        create_primary = await client.post(
            "/admin/api/providers",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "name": "Primary Provider",
                "slug": "primary-provider",
                "base_url": "https://primary.example.com",
                "api_keys": ["sk-primary-1"],
                "enabled": True,
                "is_default": True,
            },
        )
        create_secondary = await client.post(
            "/admin/api/providers",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "name": "Secondary Provider",
                "slug": "secondary-provider",
                "base_url": "https://secondary.example.com",
                "api_keys": ["sk-secondary-1"],
                "enabled": True,
                "is_default": False,
            },
        )
        update_response = await client.put(
            "/admin/api/providers/secondary-provider",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "name": "Secondary Provider Updated",
                "enabled": False,
            },
        )
        set_default_response = await client.post(
            "/admin/api/providers/secondary-provider/set-default",
            headers={"Authorization": "Bearer secret-token"},
        )
        list_after_default = await client.get(
            "/admin/api/providers",
            headers={"Authorization": "Bearer secret-token"},
        )
        delete_response = await client.delete(
            "/admin/api/providers/secondary-provider",
            headers={"Authorization": "Bearer secret-token"},
        )
        list_after_delete = await client.get(
            "/admin/api/providers",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert create_primary.status_code == 201
    assert create_secondary.status_code == 201
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Secondary Provider Updated"
    assert update_response.json()["enabled"] is False

    assert set_default_response.status_code == 200
    assert set_default_response.json()["slug"] == "secondary-provider"
    assert set_default_response.json()["is_default"] is True

    providers_after_default = {item["slug"]: item for item in list_after_default.json()["items"]}
    assert providers_after_default["primary-provider"]["is_default"] is False
    assert providers_after_default["secondary-provider"]["is_default"] is True

    assert delete_response.status_code == 200
    assert delete_response.json() == {"detail": "Provider deleted successfully"}
    remaining_items = list_after_delete.json()["items"]
    assert [item["slug"] for item in remaining_items] == ["primary-provider"]


@pytest.mark.asyncio
async def test_provider_management_api_can_get_and_update_existing_api_keys(monkeypatch):
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        create_response = await client.post(
            "/admin/api/providers",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "name": "Key Provider",
                "slug": "key-provider",
                "base_url": "https://keys.example.com",
                "api_keys": ["sk-key-1", "sk-key-2"],
                "enabled": True,
                "is_default": False,
            },
        )
        detail_response = await client.get(
            "/admin/api/providers/key-provider",
            headers={"Authorization": "Bearer secret-token"},
        )
        update_response = await client.put(
            "/admin/api/providers/key-provider",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "api_keys": ["sk-key-1", "sk-key-3", "sk-key-4"],
            },
        )
        detail_after_update = await client.get(
            "/admin/api/providers/key-provider",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert create_response.status_code == 201
    assert detail_response.status_code == 200
    assert detail_response.json()["api_keys"] == ["sk-key-1", "sk-key-2"]
    assert update_response.status_code == 200
    assert update_response.json()["api_key_count"] == 3
    assert detail_after_update.status_code == 200
    assert detail_after_update.json()["api_keys"] == ["sk-key-1", "sk-key-3", "sk-key-4"]