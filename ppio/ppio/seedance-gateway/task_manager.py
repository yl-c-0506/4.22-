import os
import json
import time
import asyncio
import logging
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, Set

from redis import asyncio as aioredis
from models import TaskStatus, TaskStatusResponse
from seedance_client import SeedanceClient

logger = logging.getLogger("task-manager")


class RedisClient(Protocol):
    async def setex(self, key: str, ttl: int, value: str) -> None: ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None: ...

    async def delete(self, key: str) -> int: ...

    async def rpush(self, key: str, value: str) -> int: ...

    async def blpop(self, key: str, timeout: int = 0) -> tuple[str, str] | None: ...

    async def sadd(self, key: str, value: str) -> int: ...

    async def srem(self, key: str, value: str) -> int: ...

    async def smembers(self, key: str) -> Set[str]: ...

    async def ping(self) -> bool: ...

class TaskManager:
    def __init__(
        self,
        redis_url: str | None,
        seedance_client: SeedanceClient,
        redis_client: RedisClient | None = None,
        task_scheduler: Callable[[Awaitable[Any]], Any] | None = None,
        client_resolver: Callable[[str | None], SeedanceClient | Awaitable[SeedanceClient]] | None = None,
        poll_interval: int | None = None,
        timeout: int | None = None,
        execution_mode: str | None = None,
        queue_key: str | None = None,
        pending_set_key: str | None = None,
        recovery_lock_key: str | None = None,
        max_concurrent_tasks: int | None = None,
    ):
        if redis_client is not None:
            self.redis = redis_client
        elif redis_url:
            self.redis = aioredis.from_url(redis_url, decode_responses=True)
        else:
            raise ValueError("redis_url or redis_client is required")
        self.client = seedance_client
        self.client_resolver = client_resolver
        self.task_scheduler = task_scheduler or asyncio.create_task
        self.poll_interval = poll_interval if poll_interval is not None else int(os.getenv("TASK_POLL_INTERVAL", 5))
        self.timeout = timeout if timeout is not None else int(os.getenv("TASK_TIMEOUT", 300))
        self.execution_mode = (execution_mode or os.getenv("TASK_EXECUTION_MODE", "inline")).strip().lower()
        if self.execution_mode not in {"inline", "queue"}:
            raise ValueError("TASK_EXECUTION_MODE must be 'inline' or 'queue'")

        self.queue_key = queue_key or os.getenv("TASK_QUEUE_KEY", "seedance:task_queue")
        self.pending_set_key = pending_set_key or os.getenv("TASK_PENDING_SET_KEY", "seedance:pending_tasks")
        self.recovery_lock_key = recovery_lock_key or os.getenv("TASK_RECOVERY_LOCK_KEY", "seedance:recovery_lock")
        self._running_tasks: set[str] = set()
        max_concurrency = max_concurrent_tasks if max_concurrent_tasks is not None else int(os.getenv("MAX_CONCURRENT_TASKS", 20))
        self.max_concurrent_tasks = max(1, max_concurrency)
        self._worker_semaphore = asyncio.Semaphore(self.max_concurrent_tasks)
        self._recovery_lock_value: str | None = None

    @staticmethod
    def _normalize_provider_slug(provider_slug: str | None) -> str:
        return provider_slug or "default"

    def _task_key(self, task_id: str, provider_slug: str | None = None) -> str:
        normalized_provider_slug = self._normalize_provider_slug(provider_slug)
        return f"task:{normalized_provider_slug}:{task_id}"

    def _task_ref(self, task_id: str, provider_slug: str | None = None) -> str:
        normalized_provider_slug = self._normalize_provider_slug(provider_slug)
        if normalized_provider_slug == "default":
            return task_id
        return json.dumps({"provider_slug": normalized_provider_slug, "task_id": task_id}, sort_keys=True)

    def _parse_task_ref(self, task_ref: str) -> tuple[str, str]:
        if task_ref.startswith("{"):
            payload = json.loads(task_ref)
            return payload["provider_slug"], payload["task_id"]
        return "default", task_ref

    async def health_check(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except Exception:
            return False

    async def _enqueue_task(self, task_id: str, provider_slug: str | None = None) -> None:
        task_ref = self._task_ref(task_id, provider_slug)
        await self.redis.sadd(self.pending_set_key, task_ref)
        await self.redis.rpush(self.queue_key, task_ref)

    async def _mark_task_complete(self, task_id: str, provider_slug: str | None = None) -> None:
        task_ref = self._task_ref(task_id, provider_slug)
        await self.redis.srem(self.pending_set_key, task_ref)

    async def _acquire_recovery_lock(self, ttl_seconds: int = 30) -> bool:
        lock_value = f"{os.getpid()}:{time.time()}"
        try:
            acquired = bool(await self.redis.set(self.recovery_lock_key, lock_value, ex=ttl_seconds, nx=True))
            if acquired:
                self._recovery_lock_value = lock_value
            return acquired
        except Exception:
            return False

    async def _release_recovery_lock(self) -> None:
        # Keep lock release passive (TTL-based) to avoid non-atomic get+delete races
        # that can accidentally remove another worker's lock after key expiration/reacquire.
        self._recovery_lock_value = None

    async def requeue_pending_tasks(self) -> int:
        has_lock = await self._acquire_recovery_lock()
        if not has_lock:
            logger.info("Skip pending task recovery because another worker is recovering")
            return 0

        pending_task_ids = await self.redis.smembers(self.pending_set_key)
        try:
            for task_id in pending_task_ids:
                await self.redis.rpush(self.queue_key, task_id)
            return len(pending_task_ids)
        finally:
            await self._release_recovery_lock()

    async def pop_next_task(self, timeout: int = 5) -> str | None:
        result = await self.redis.blpop(self.queue_key, timeout=timeout)
        if not result:
            return None

        _, task_id = result
        if isinstance(task_id, bytes):
            return task_id.decode("utf-8")
        return task_id

    async def _run_task_with_tracking(self, task_id: str, provider_slug: str | None = None) -> None:
        task_ref = self._task_ref(task_id, provider_slug)
        try:
            async with self._worker_semaphore:
                await self._poll_task_loop(task_id, provider_slug=provider_slug)
        finally:
            self._running_tasks.discard(task_ref)

    async def _resolve_client(self, provider_slug: str | None = None) -> SeedanceClient:
        if self.client_resolver is None:
            return self.client

        maybe_client = self.client_resolver(provider_slug)
        if inspect.isawaitable(maybe_client):
            return await maybe_client
        return maybe_client

    def _schedule_task(self, task_id: str, provider_slug: str | None = None) -> None:
        task_ref = self._task_ref(task_id, provider_slug)
        if task_ref in self._running_tasks:
            return

        self._running_tasks.add(task_ref)
        self.task_scheduler(self._run_task_with_tracking(task_id, provider_slug=provider_slug))

    async def run_worker(self, pop_timeout: int = 5) -> None:
        recovered_count = await self.requeue_pending_tasks()
        if recovered_count:
            logger.info(f"Recovered {recovered_count} pending tasks back to queue")

        while True:
            if len(self._running_tasks) >= self.max_concurrent_tasks:
                await asyncio.sleep(0.1)
                continue

            task_id = await self.pop_next_task(timeout=pop_timeout)
            if not task_id:
                continue
            provider_slug, parsed_task_id = self._parse_task_ref(task_id)
            self._schedule_task(parsed_task_id, provider_slug=provider_slug)

    async def create_task(self, task_id: str, prompt: str | None, provider_slug: str | None = None):
        """初始化任务存入 Redis"""
        normalized_provider_slug = self._normalize_provider_slug(provider_slug)
        task_data = {
            "id": task_id,
            "provider_slug": normalized_provider_slug,
            "status": TaskStatus.QUEUED,
            "prompt": prompt,
            "result_url": None,
            "error": None,
            "progress": 0,
            "created_at": time.time()
        }
        await self.redis.setex(self._task_key(task_id, normalized_provider_slug), self.timeout + 60, json.dumps(task_data))

        if self.execution_mode == "queue":
            await self._enqueue_task(task_id, normalized_provider_slug)
            return

        # inline 模式下由 API 进程直接启动后台轮询
        self._schedule_task(task_id, provider_slug=normalized_provider_slug)

    async def get_task(self, task_id: str, provider_slug: str | None = None) -> TaskStatusResponse:
        """获取任务状态"""
        normalized_provider_slug = self._normalize_provider_slug(provider_slug)
        data = await self.redis.get(self._task_key(task_id, normalized_provider_slug))
        if not data:
            return TaskStatusResponse(id=task_id, status=TaskStatus.FAILED, error="Task not found")
        return TaskStatusResponse(**json.loads(data))

    async def _update_task(self, task_id: str, provider_slug: str | None = None, **kwargs):
        """更新任务字段"""
        normalized_provider_slug = self._normalize_provider_slug(provider_slug)
        data = await self.redis.get(self._task_key(task_id, normalized_provider_slug))
        if data:
            task = json.loads(data)
            task.update(kwargs)
            await self.redis.setex(self._task_key(task_id, normalized_provider_slug), self.timeout + 60, json.dumps(task))

    async def _poll_task_loop(self, task_id: str, provider_slug: str | None = None):
        """后台轮询循环"""
        normalized_provider_slug = self._normalize_provider_slug(provider_slug)
        client = await self._resolve_client(normalized_provider_slug)
        start_time = time.time()
        try:
            while True:
                # 检查超时
                if time.time() - start_time > self.timeout:
                    await self._update_task(task_id, provider_slug=normalized_provider_slug, status=TaskStatus.FAILED, error="Task timeout")
                    await self._mark_task_complete(task_id, normalized_provider_slug)
                    logger.error(f"Task {task_id} timeout")
                    break

                # 轮询 Seedance
                status, url, progress, err = await client.poll_task(task_id)

                if status:
                    update_fields = {
                        "status": status,
                        "result_url": url,
                        "progress": progress,
                    }
                    if status == TaskStatus.FAILED and err:
                        update_fields["error"] = err
                    elif status == TaskStatus.SUCCESS:
                        update_fields["error"] = None

                    await self._update_task(task_id, provider_slug=normalized_provider_slug, **update_fields)

                    if status == TaskStatus.SUCCESS or status == TaskStatus.FAILED:
                        await self._mark_task_complete(task_id, normalized_provider_slug)
                        logger.info(f"Task {task_id} finished with status: {status}")
                        break

                if err:
                    if err == "No available keys":
                        await self._update_task(task_id, provider_slug=normalized_provider_slug, status=TaskStatus.FAILED, error=err)
                        await self._mark_task_complete(task_id, normalized_provider_slug)
                        logger.error(f"Task {task_id} failed: {err}")
                        break

                    await asyncio.sleep(self.poll_interval)
                    continue

                await asyncio.sleep(self.poll_interval)
        except Exception as exc:
            logger.exception(f"Task {task_id} polling failed")
            await self._update_task(task_id, provider_slug=normalized_provider_slug, status=TaskStatus.FAILED, error=str(exc))
            await self._mark_task_complete(task_id, normalized_provider_slug)
