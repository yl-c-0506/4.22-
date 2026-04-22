import hashlib
import hmac
import os
import logging
import time
from typing import Set
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Header, status
from fastapi.responses import HTMLResponse, JSONResponse
from contextlib import asynccontextmanager

from models import (
    ChatMessage,
    OpenAIChatCompletionsRequest,
    OpenAIVideoRequest,
    ProviderCreateRequest,
    ProviderUpdateRequest,
    SeedanceTaskRequest,
    TaskStatusResponse,
)
from provider_store import ProviderAlreadyExistsError, ProviderNotFoundError, ProviderStore
from seedance_client import SeedanceClient
from task_manager import TaskManager

# ================= 初始化 =================
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seedance-gateway")

seedance_client = None
task_manager = None
provider_store = None
DEFAULT_GATEWAY_PUBLIC_URL = "http://localhost:8001"
DEFAULT_TASK_STATUS_URL_TTL = 360
FAST_MODEL_ALIASES = {"seedance-2.0-fast", "seedance-fast"}


class InMemoryProviderRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> int:
        if key in self.values:
            del self.values[key]
            return 1
        return 0

    async def sadd(self, key: str, value: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.add(value)
        return 1 if len(bucket) > before else 0

    async def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    async def srem(self, key: str, value: str) -> int:
        bucket = self.sets.get(key)
        if bucket and value in bucket:
            bucket.remove(value)
            return 1
        return 0


def load_runtime_config() -> tuple[list[str], str, str, str]:
    required_env = {
        "SEEDANCE_API_KEYS": os.getenv("SEEDANCE_API_KEYS"),
        "SEEDANCE_BASE_URL": os.getenv("SEEDANCE_BASE_URL"),
        "REDIS_URL": os.getenv("REDIS_URL"),
        "GATEWAY_ACCESS_TOKEN": os.getenv("GATEWAY_ACCESS_TOKEN"),
    }
    missing = [name for name, value in required_env.items() if not value]
    if missing:
        missing_names = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {missing_names}")

    keys = [key.strip() for key in required_env["SEEDANCE_API_KEYS"].split(",") if key.strip()]
    if not keys:
        raise RuntimeError("SEEDANCE_API_KEYS contains no valid keys")

    return (
        keys,
        required_env["SEEDANCE_BASE_URL"],
        required_env["REDIS_URL"],
        required_env["GATEWAY_ACCESS_TOKEN"],
    )


def build_seedance_request(request: OpenAIVideoRequest) -> SeedanceTaskRequest:
    request_payload = request.model_dump(exclude_none=True)
    model_name = request_payload.pop("model", request.model)
    fast_value = request_payload.pop("fast", None)
    if fast_value is None:
        fast_value = model_name in FAST_MODEL_ALIASES
    request_payload["fast"] = fast_value
    return SeedanceTaskRequest(**request_payload)


def clone_request_with_prompt(
    request: OpenAIVideoRequest | OpenAIChatCompletionsRequest,
    prompt: str,
) -> OpenAIVideoRequest | OpenAIChatCompletionsRequest:
    if hasattr(request, "model_copy"):
        return request.model_copy(update={"prompt": prompt})
    return request.copy(update={"prompt": prompt})


def extract_text_from_message(message: ChatMessage) -> str | None:
    if isinstance(message.content, str):
        content = message.content.strip()
        return content or None

    if isinstance(message.content, list):
        text_parts: list[str] = []
        for part in message.content:
            if part.get("type") != "text":
                continue
            text_value = part.get("text")
            if isinstance(text_value, str) and text_value.strip():
                text_parts.append(text_value.strip())
        if text_parts:
            return "\n".join(text_parts)

    return None


def extract_user_prompt(messages: list[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if message.role != "user":
            continue
        prompt = extract_text_from_message(message)
        if prompt:
            return prompt
    return None


def build_task_status_url(task_id: str, provider_slug: str | None = None) -> str:
    public_url = os.getenv("GATEWAY_PUBLIC_URL", DEFAULT_GATEWAY_PUBLIC_URL).rstrip("/")
    expires_at = int(time.time()) + int(os.getenv("TASK_STATUS_URL_TTL", DEFAULT_TASK_STATUS_URL_TTL))
    status_token = build_task_status_token(task_id, expires_at, provider_slug)

    if provider_slug:
        return (
            f"{public_url}/v1/providers/{provider_slug}/tasks/{task_id}"
            f"?expires_at={expires_at}&status_token={status_token}"
        )

    return f"{public_url}/v1/tasks/{task_id}?expires_at={expires_at}&status_token={status_token}"


def get_gateway_access_token() -> str:
    gateway_access_token = os.getenv("GATEWAY_ACCESS_TOKEN")
    if not gateway_access_token:
        raise HTTPException(status_code=500, detail="Gateway access token is not configured")
    return gateway_access_token


def build_task_status_token(task_id: str, expires_at: int, provider_slug: str | None = None) -> str:
    gateway_access_token = get_gateway_access_token()
    if provider_slug:
        payload = f"{provider_slug}:{task_id}:{expires_at}"
    else:
        payload = f"{task_id}:{expires_at}"
    return hmac.new(
        gateway_access_token.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_task_status_access(
    task_id: str,
    authorization: str | None,
    status_token: str | None,
    expires_at: int | None,
    provider_slug: str | None = None,
) -> None:
    gateway_access_token = get_gateway_access_token()
    expected = f"Bearer {gateway_access_token}"
    if authorization == expected:
        return

    if (
        status_token
        and expires_at is not None
        and expires_at >= int(time.time())
        and hmac.compare_digest(status_token, build_task_status_token(task_id, expires_at, provider_slug))
    ):
        return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def get_provider_store() -> ProviderStore:
    global provider_store
    if provider_store is None:
        public_url = os.getenv("GATEWAY_PUBLIC_URL", DEFAULT_GATEWAY_PUBLIC_URL)
        provider_store = ProviderStore(InMemoryProviderRedis(), public_url)
    return provider_store


async def resolve_provider_client(provider_slug: str | None) -> tuple[SeedanceClient, str | None]:
    if provider_slug:
        store = await get_provider_store()
        try:
            provider = await store.get_provider(provider_slug)
        except ProviderNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_slug}' not found") from exc

        if not provider.enabled:
            raise HTTPException(status_code=400, detail=f"Provider '{provider_slug}' is disabled")

        return SeedanceClient(provider.api_keys, provider.base_url), provider.slug

    if seedance_client is None:
        raise HTTPException(status_code=503, detail="Gateway not initialized")

    return seedance_client, None


async def submit_seedance_task(
    seed_req: SeedanceTaskRequest,
    prompt: str | None,
    provider_slug: str | None = None,
    provider_client: SeedanceClient | None = None,
) -> tuple[str, str]:
    prompt_preview = prompt[:50] if prompt else "<non-text request>"
    logger.info(f"Submitting task with prompt: {prompt_preview}...")
    selected_client = provider_client or seedance_client
    real_task_id, err = await selected_client.submit_task(seed_req)

    if err:
        logger.error(f"Failed to submit: {err}")
        raise HTTPException(status_code=502, detail=f"Upstream error: {err}")

    if not real_task_id:
        logger.error("Failed to submit: missing task id from upstream response")
        raise HTTPException(status_code=502, detail="Upstream error: Missing task id from upstream response")

    await task_manager.create_task(real_task_id, prompt, provider_slug=provider_slug)
    return real_task_id, build_task_status_url(real_task_id, provider_slug=provider_slug)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global seedance_client, task_manager, provider_store
    # 初始化组件
    keys, base_url, redis_url, _ = load_runtime_config()
    seedance_client = SeedanceClient(keys, base_url)
    task_manager = TaskManager(redis_url, seedance_client)
    provider_store = ProviderStore(task_manager.redis, os.getenv("GATEWAY_PUBLIC_URL", DEFAULT_GATEWAY_PUBLIC_URL))
    yield
    # 清理
    pass

app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if task_manager is None or seedance_client is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    if not await task_manager.health_check():
        raise HTTPException(status_code=503, detail="Redis unavailable")

    return {"status": "ready"}


# 鉴权依赖
def verify_token(authorization: str | None = Header(None)) -> None:
    gateway_access_token = get_gateway_access_token()
    expected = f"Bearer {gateway_access_token}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
        return """
<!DOCTYPE html>
<html>
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Provider Manager</title>
    <style>
        :root { --bg:#f7f6f2; --ink:#222; --accent:#1144aa; --panel:#fff; --line:#ddd; }
        body{margin:0;padding:24px;background:linear-gradient(135deg,#efe9d5 0%,#f7f6f2 50%,#e6edf9 100%);font-family:Georgia,\"Times New Roman\",serif;color:var(--ink)}
        .wrap{max-width:1100px;margin:0 auto}
        .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
        .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:0 10px 30px rgba(0,0,0,.06)}
        input,textarea{width:100%;padding:10px;border:1px solid #bbb;border-radius:10px;font-family:monospace}
        button{background:var(--accent);color:#fff;border:none;padding:10px 14px;border-radius:10px;cursor:pointer}
        h1{margin:0 0 14px 0}
        .muted{color:#666;font-size:13px}
        .item{padding:10px;border:1px dashed #bbb;border-radius:10px;margin-top:8px}
        @media (max-width: 900px){.grid{grid-template-columns:1fr}}
    </style>
</head>
<body>
    <div class=\"wrap\">
        <h1>Provider Manager</h1>
        <p class=\"muted\">录入 base_url 与多组 api_keys，即可生成对应封装接口。</p>
        <div class=\"grid\">
            <div class=\"card\">
                <h3>新增 Provider</h3>
                <label>网关令牌</label><input id=\"token\" placeholder=\"Bearer token (仅输入token内容)\" />
                <label>name</label><input id=\"name\" placeholder=\"Demo Provider\" />
                <label>slug</label><input id=\"slug\" placeholder=\"demo-provider\" />
                <label>base_url</label><input id=\"base_url\" placeholder=\"https://api.example.com\" />
                <label>api_keys（每行一个）</label><textarea id=\"api_keys\" rows=\"4\"></textarea>
                <label><input id=\"is_default\" type=\"checkbox\" /> 设为默认 provider</label>
                <div style=\"margin-top:10px\"><button onclick=\"createProvider()\">保存并封装</button></div>
            </div>
            <div class=\"card\">
                <h3>Provider 列表</h3>
                <div style=\"margin-bottom:8px\"><button onclick=\"loadProviders()\">刷新</button></div>
                <div id=\"provider-list\"></div>
            </div>
        </div>
    </div>
    <script>
        function headers(){
            const token = document.getElementById('token').value.trim();
            return { 'Content-Type':'application/json', 'Authorization':'Bearer '+token };
        }

        async function loadProviders(){
            const target = document.getElementById('provider-list');
            target.innerHTML = '加载中...';
            const res = await fetch('/admin/api/providers', { headers: headers() });
            if(!res.ok){ target.innerHTML = '加载失败: '+(await res.text()); return; }
            const data = await res.json();
            if(!data.items.length){ target.innerHTML = '<div class=\"muted\">暂无 provider</div>'; return; }
            target.innerHTML = data.items.map(item => `
                <div class=\"item\">
                    <div><strong>${item.name}</strong> (${item.slug}) ${item.is_default ? ' [default]' : ''}</div>
                    <div class=\"muted\">base_url: ${item.base_url}</div>
                    <div class=\"muted\">video: ${item.video_generation_url}</div>
                    <div class=\"muted\">chat: ${item.chat_completions_url}</div>
                    <div class=\"muted\">task: ${item.task_status_url_template}</div>
                    <div class=\"muted\">api keys: ${item.api_key_count}</div>
                </div>
            `).join('');
        }

        async function createProvider(){
            const payload = {
                name: document.getElementById('name').value.trim(),
                slug: document.getElementById('slug').value.trim(),
                base_url: document.getElementById('base_url').value.trim(),
                api_keys: document.getElementById('api_keys').value.split('\n').map(v => v.trim()).filter(Boolean),
                enabled: true,
                is_default: document.getElementById('is_default').checked
            };

            const res = await fetch('/admin/api/providers', {
                method:'POST',
                headers: headers(),
                body: JSON.stringify(payload)
            });

            if(!res.ok){ alert('保存失败: '+(await res.text())); return; }
            await loadProviders();
        }

        loadProviders();
    </script>
</body>
</html>
        """


@app.get("/admin/api/providers", dependencies=[Depends(verify_token)])
async def list_providers_api():
        store = await get_provider_store()
        providers = await store.list_providers()
        return providers.model_dump()


@app.post("/admin/api/providers", status_code=201, dependencies=[Depends(verify_token)])
async def create_provider_api(request: ProviderCreateRequest):
        store = await get_provider_store()
        try:
                provider_summary = await store.create_provider(request)
        except ProviderAlreadyExistsError as exc:
                raise HTTPException(status_code=409, detail=f"Provider '{request.slug}' already exists") from exc

        return provider_summary.model_dump()


@app.post("/admin/api/providers/{provider_slug}/set-default", dependencies=[Depends(verify_token)])
async def set_default_provider_api(provider_slug: str):
        store = await get_provider_store()
        try:
                summary = await store.set_default_provider(provider_slug)
        except ProviderNotFoundError as exc:
                raise HTTPException(status_code=404, detail=f"Provider '{provider_slug}' not found") from exc
        return summary.model_dump()

# ================= 路由：OpenAI 兼容接口 =================

@app.post("/v1/video/generations", dependencies=[Depends(verify_token)])
async def create_video(request: OpenAIVideoRequest):
    """
    对外暴露 OpenAI 兼容的接口
    1. 接收 New API 的请求
    2. 转换格式提交给 Seedance
    3. 立即返回 Task ID (模拟 OpenAI 的异步风格)
    """
    # 1. 协议转换：OpenAI Request -> Seedance Request
    seed_req = build_seedance_request(request)
    provider_client, provider_slug = await resolve_provider_client(None)
    
    # 3. 提交给 Seedance
    real_task_id, status_url = await submit_seedance_task(
        seed_req,
        request.prompt,
        provider_slug=provider_slug,
        provider_client=provider_client,
    )

    # 5. 返回 OpenAI 风格的响应 (把 real_task_id 返回给 New API)
    return JSONResponse(content={
        "object": "list",
        "data": [{"url": status_url}],
        "model": request.model,
        "id": real_task_id
    }, status_code=202) # 202 Accepted


# New API custom channels normalize the configured URL to /v1 and then POST there.
@app.post("/v1/chat/completions", dependencies=[Depends(verify_token)])
@app.post("/v1", dependencies=[Depends(verify_token)])
async def create_chat_completion(request: OpenAIChatCompletionsRequest):
    prompt = request.prompt or extract_user_prompt(request.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="A user message is required to create a video task")

    request_with_prompt = clone_request_with_prompt(request, prompt)
    seed_req = build_seedance_request(request_with_prompt)
    provider_client, provider_slug = await resolve_provider_client(None)
    real_task_id, status_url = await submit_seedance_task(
        seed_req,
        prompt,
        provider_slug=provider_slug,
        provider_client=provider_client,
    )
    created_at = int(time.time())
    assistant_message = (
        "Video generation task submitted successfully.\n"
        f"Task ID: {real_task_id}\n"
        f"Status URL: {status_url}"
    )

    return JSONResponse(content={
        "id": f"chatcmpl-{real_task_id}",
        "object": "chat.completion",
        "created": created_at,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": assistant_message,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    })

@app.get("/v1/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    status_token: str | None = None,
    expires_at: int | None = None,
    authorization: str | None = Header(None),
):
    """
    任务状态 URL 带有任务级签名，因此调用方可直接轮询而无需暴露全局网关令牌。
    """
    verify_task_status_access(task_id, authorization, status_token, expires_at, provider_slug=None)
    return await task_manager.get_task(task_id)


@app.post("/v1/providers/{provider_slug}/video/generations", dependencies=[Depends(verify_token)])
async def create_video_by_provider(provider_slug: str, request: OpenAIVideoRequest):
    seed_req = build_seedance_request(request)
    provider_client, normalized_provider_slug = await resolve_provider_client(provider_slug)
    real_task_id, status_url = await submit_seedance_task(
        seed_req,
        request.prompt,
        provider_slug=normalized_provider_slug,
        provider_client=provider_client,
    )

    return JSONResponse(content={
        "object": "list",
        "data": [{"url": status_url}],
        "model": request.model,
        "id": real_task_id,
    }, status_code=202)


@app.post("/v1/providers/{provider_slug}/chat/completions", dependencies=[Depends(verify_token)])
async def create_chat_completion_by_provider(provider_slug: str, request: OpenAIChatCompletionsRequest):
    prompt = request.prompt or extract_user_prompt(request.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="A user message is required to create a video task")

    request_with_prompt = clone_request_with_prompt(request, prompt)
    seed_req = build_seedance_request(request_with_prompt)
    provider_client, normalized_provider_slug = await resolve_provider_client(provider_slug)
    real_task_id, status_url = await submit_seedance_task(
        seed_req,
        prompt,
        provider_slug=normalized_provider_slug,
        provider_client=provider_client,
    )

    created_at = int(time.time())
    assistant_message = (
        "Video generation task submitted successfully.\n"
        f"Task ID: {real_task_id}\n"
        f"Status URL: {status_url}"
    )
    return JSONResponse(content={
        "id": f"chatcmpl-{real_task_id}",
        "object": "chat.completion",
        "created": created_at,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": assistant_message,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    })


@app.get("/v1/providers/{provider_slug}/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status_by_provider(
    provider_slug: str,
    task_id: str,
    status_token: str | None = None,
    expires_at: int | None = None,
    authorization: str | None = Header(None),
):
    verify_task_status_access(task_id, authorization, status_token, expires_at, provider_slug=provider_slug)
    return await task_manager.get_task(task_id, provider_slug=provider_slug)
