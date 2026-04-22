import json

import httpx
import pytest

from models import SeedanceTaskRequest, TaskStatus
from seedance_client import SeedanceClient


def test_get_api_key_skips_failed_keys_and_trims_whitespace():
    client = SeedanceClient([" key-a ", "key-b"], "https://seedance.example")
    client._mark_key_failed("key-a")

    assert client._get_api_key() == "key-b"


@pytest.mark.asyncio
async def test_submit_task_returns_upstream_task_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer key-a"
        assert request.url.path == "/v3/async/seedance-2.0"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "prompt": "generate a short clip",
            "fast": True,
            "resolution": "720p",
        }
        return httpx.Response(200, json={"task_id": "upstream-task-1"})

    client = SeedanceClient(
        ["key-a"],
        "https://seedance.example",
        transport=httpx.MockTransport(handler),
    )

    task_id, error = await client.submit_task(
        SeedanceTaskRequest(prompt="generate a short clip", fast=True, resolution="720p")
    )

    assert task_id == "upstream-task-1"
    assert error is None


@pytest.mark.asyncio
async def test_submit_task_rejects_success_response_without_task_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/async/seedance-2.0"
        return httpx.Response(200, json={"status": "accepted"})

    client = SeedanceClient(
        ["key-a"],
        "https://seedance.example",
        transport=httpx.MockTransport(handler),
    )

    task_id, error = await client.submit_task(
        SeedanceTaskRequest(prompt="generate a short clip", fast=True)
    )

    assert task_id is None
    assert error == "Seedance response missing task_id"


@pytest.mark.asyncio
async def test_submit_task_retries_with_next_key_after_rate_limit():
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_header = request.headers["Authorization"]
        seen_tokens.append(auth_header)
        if auth_header == "Bearer key-a":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"task_id": "upstream-task-2"})

    client = SeedanceClient(
        ["key-a", "key-b"],
        "https://seedance.example",
        transport=httpx.MockTransport(handler),
    )

    task_id, error = await client.submit_task(
        SeedanceTaskRequest(prompt="generate a short clip", fast=True)
    )

    assert task_id == "upstream-task-2"
    assert error is None
    assert seen_tokens == ["Bearer key-a", "Bearer key-b"]


@pytest.mark.asyncio
async def test_submit_task_does_not_permanently_blacklist_rate_limited_key():
    key_a_calls = 0
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal key_a_calls

        auth_header = request.headers["Authorization"]
        seen_tokens.append(auth_header)
        if auth_header == "Bearer key-a":
            key_a_calls += 1
            if key_a_calls == 1:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json={"task_id": "upstream-task-3"})
        return httpx.Response(200, json={"task_id": "upstream-task-2"})

    client = SeedanceClient(
        ["key-a", "key-b"],
        "https://seedance.example",
        transport=httpx.MockTransport(handler),
    )

    first_task_id, first_error = await client.submit_task(
        SeedanceTaskRequest(prompt="generate a short clip", fast=True)
    )
    second_task_id, second_error = await client.submit_task(
        SeedanceTaskRequest(prompt="generate another short clip", fast=True)
    )

    assert first_task_id == "upstream-task-2"
    assert first_error is None
    assert second_task_id == "upstream-task-3"
    assert second_error is None
    assert seen_tokens == ["Bearer key-a", "Bearer key-b", "Bearer key-a"]


@pytest.mark.asyncio
async def test_submit_task_permanently_skips_unauthorized_key():
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_header = request.headers["Authorization"]
        seen_tokens.append(auth_header)
        if auth_header == "Bearer key-a":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"task_id": "upstream-task-4"})

    client = SeedanceClient(
        ["key-a", "key-b"],
        "https://seedance.example",
        transport=httpx.MockTransport(handler),
    )

    first_task_id, first_error = await client.submit_task(
        SeedanceTaskRequest(prompt="generate a short clip", fast=True)
    )
    second_task_id, second_error = await client.submit_task(
        SeedanceTaskRequest(prompt="generate another short clip", fast=True)
    )

    assert first_task_id == "upstream-task-4"
    assert first_error is None
    assert second_task_id == "upstream-task-4"
    assert second_error is None
    assert client.failed_keys == {"key-a"}
    assert seen_tokens == ["Bearer key-a", "Bearer key-b", "Bearer key-b"]


@pytest.mark.asyncio
async def test_poll_task_maps_ppio_success_status_to_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/async/task-result"
        assert request.url.params["task_id"] == "upstream-task-1"
        return httpx.Response(
            200,
            json={
                "task": {
                    "task_id": "upstream-task-1",
                    "status": "TASK_STATUS_SUCCEED",
                    "progress_percent": 100,
                },
                "videos": [
                    {
                        "video_url": "https://cdn.example/video.mp4",
                        "video_url_ttl": "600",
                        "video_type": "mp4",
                    }
                ],
            },
        )

    client = SeedanceClient(
        ["key-a"],
        "https://seedance.example",
        transport=httpx.MockTransport(handler),
    )

    status, result_url, progress, error = await client.poll_task("upstream-task-1")

    assert status == TaskStatus.SUCCESS
    assert result_url == "https://cdn.example/video.mp4"
    assert progress == 100
    assert error is None


@pytest.mark.asyncio
async def test_poll_task_retries_with_next_key_after_unauthorized():
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_header = request.headers["Authorization"]
        seen_tokens.append(auth_header)
        if auth_header == "Bearer key-a":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(
            200,
            json={
                "task": {
                    "task_id": "upstream-task-3",
                    "status": "TASK_STATUS_SUCCEED",
                    "progress_percent": 100,
                },
                "videos": [{"video_url": "https://cdn.example/video-3.mp4"}],
            },
        )

    client = SeedanceClient(
        ["key-a", "key-b"],
        "https://seedance.example",
        transport=httpx.MockTransport(handler),
    )

    status, result_url, progress, error = await client.poll_task("upstream-task-3")

    assert status == TaskStatus.SUCCESS
    assert result_url == "https://cdn.example/video-3.mp4"
    assert progress == 100
    assert error is None
    assert client.failed_keys == {"key-a"}
    assert seen_tokens == ["Bearer key-a", "Bearer key-b"]


@pytest.mark.asyncio
async def test_poll_task_returns_failure_reason_from_ppio_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/async/task-result"
        assert request.url.params["task_id"] == "upstream-task-2"
        return httpx.Response(
            200,
            json={
                "task": {
                    "task_id": "upstream-task-2",
                    "status": "TASK_STATUS_FAILED",
                    "reason": "content policy rejected",
                    "progress_percent": 65,
                },
                "videos": [],
            },
        )

    client = SeedanceClient(
        ["key-a"],
        "https://seedance.example",
        transport=httpx.MockTransport(handler),
    )

    status, result_url, progress, error = await client.poll_task("upstream-task-2")

    assert status == TaskStatus.FAILED
    assert result_url is None
    assert progress == 65
    assert error == "content policy rejected"
