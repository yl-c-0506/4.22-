import pytest

import task_manager
from models import TaskStatus
from task_manager import TaskManager


class InMemoryRedis:
    def __init__(self):
        self.store = {}
        self.lists = {}
        self.sets = {}

    async def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key: str):
        if key in self.store:
            del self.store[key]
            return 1
        return 0

    async def rpush(self, key: str, value: str):
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def blpop(self, key: str, timeout: int = 0):
        bucket = self.lists.setdefault(key, [])
        if not bucket:
            return None
        value = bucket.pop(0)
        return key, value

    async def sadd(self, key: str, value: str):
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.add(value)
        return 1 if len(bucket) > before else 0

    async def srem(self, key: str, value: str):
        bucket = self.sets.setdefault(key, set())
        if value in bucket:
            bucket.remove(value)
            return 1
        return 0

    async def smembers(self, key: str):
        return set(self.sets.get(key, set()))

    async def ping(self):
        return True


class DummySeedanceClient:
    async def poll_task(self, task_id: str):
        return TaskStatus.SUCCESS, f"https://cdn.example/{task_id}.mp4", 100, None


class RaisingSeedanceClient:
    async def poll_task(self, task_id: str):
        raise RuntimeError("upstream crashed")


class FailedTaskSeedanceClient:
    async def poll_task(self, task_id: str):
        return TaskStatus.FAILED, None, 100, "content policy rejected"


class ExhaustedKeysSeedanceClient:
    async def poll_task(self, task_id: str):
        return None, None, 0, "No available keys"


def discard_background_task(coroutine):
    coroutine.close()
    return None


@pytest.mark.asyncio
async def test_create_task_persists_initial_state_without_real_redis():
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=DummySeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
    )

    await manager.create_task("task-123", "generate a short clip")
    task = await manager.get_task("task-123")

    assert task.id == "task-123"
    assert task.status == TaskStatus.QUEUED
    assert task.prompt == "generate a short clip"
    assert task.result_url is None
    assert task.error is None
    assert task.progress == 0


@pytest.mark.asyncio
async def test_get_task_returns_failed_response_when_missing():
    manager = TaskManager(
        redis_url=None,
        seedance_client=DummySeedanceClient(),
        redis_client=InMemoryRedis(),
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
    )

    task = await manager.get_task("missing-task")

    assert task.id == "missing-task"
    assert task.status == TaskStatus.FAILED
    assert task.error == "Task not found"


@pytest.mark.asyncio
async def test_poll_task_loop_updates_successful_tasks():
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=DummySeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
    )

    await manager.create_task("task-123", "generate a short clip")
    await manager._poll_task_loop("task-123")
    task = await manager.get_task("task-123")

    assert task.status == TaskStatus.SUCCESS
    assert task.result_url == "https://cdn.example/task-123.mp4"
    assert task.progress == 100
    assert task.error is None


@pytest.mark.asyncio
async def test_poll_task_loop_marks_task_failed_when_client_raises():
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=RaisingSeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
    )

    await manager.create_task("task-123", "generate a short clip")
    await manager._poll_task_loop("task-123")
    task = await manager.get_task("task-123")

    assert task.status == TaskStatus.FAILED
    assert task.error == "upstream crashed"


@pytest.mark.asyncio
async def test_poll_task_loop_persists_terminal_failure_reason(monkeypatch):
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=FailedTaskSeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=1,
    )

    await manager.create_task("task-123", "generate a short clip")
    time_values = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(task_manager.time, "time", lambda: next(time_values))

    await manager._poll_task_loop("task-123")
    task = await manager.get_task("task-123")

    assert task.status == TaskStatus.FAILED
    assert task.error == "content policy rejected"
    assert task.progress == 100


@pytest.mark.asyncio
async def test_poll_task_loop_fails_fast_when_all_keys_are_unavailable(monkeypatch):
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=ExhaustedKeysSeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=1,
    )

    await manager.create_task("task-123", "generate a short clip")
    time_values = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(task_manager.time, "time", lambda: next(time_values))

    await manager._poll_task_loop("task-123")
    task = await manager.get_task("task-123")

    assert task.status == TaskStatus.FAILED
    assert task.error == "No available keys"


@pytest.mark.asyncio
async def test_create_task_enqueue_mode_pushes_task_to_queue_without_inline_polling():
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=DummySeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
        execution_mode="queue",
        queue_key="seedance:test_queue",
        pending_set_key="seedance:test_pending",
    )

    await manager.create_task("task-123", "generate a short clip")
    task = await manager.get_task("task-123")

    assert task.status == TaskStatus.QUEUED
    assert redis.lists["seedance:test_queue"] == ["task-123"]
    assert "task-123" in redis.sets["seedance:test_pending"]


@pytest.mark.asyncio
async def test_requeue_pending_tasks_restores_all_pending_items_to_queue():
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=DummySeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
        execution_mode="queue",
        queue_key="seedance:test_queue",
        pending_set_key="seedance:test_pending",
    )

    await redis.sadd("seedance:test_pending", "task-a")
    await redis.sadd("seedance:test_pending", "task-b")

    restored = await manager.requeue_pending_tasks()

    assert restored == 2
    assert sorted(redis.lists["seedance:test_queue"]) == ["task-a", "task-b"]


@pytest.mark.asyncio
async def test_requeue_pending_tasks_skips_when_recovery_lock_is_held():
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=DummySeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
        execution_mode="queue",
        queue_key="seedance:test_queue",
        pending_set_key="seedance:test_pending",
        recovery_lock_key="seedance:test_recovery_lock",
    )

    await redis.sadd("seedance:test_pending", "task-a")
    await redis.set("seedance:test_recovery_lock", "other-worker", nx=False)

    restored = await manager.requeue_pending_tasks()

    assert restored == 0
    assert redis.lists.get("seedance:test_queue", []) == []


@pytest.mark.asyncio
async def test_release_recovery_lock_does_not_delete_foreign_lock():
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=DummySeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
        execution_mode="queue",
        recovery_lock_key="seedance:test_recovery_lock",
    )

    acquired = await manager._acquire_recovery_lock()
    assert acquired is True

    await redis.set("seedance:test_recovery_lock", "other-worker", nx=False)
    await manager._release_recovery_lock()

    assert await redis.get("seedance:test_recovery_lock") == "other-worker"


@pytest.mark.asyncio
async def test_task_ids_are_isolated_per_provider():
    redis = InMemoryRedis()
    manager = TaskManager(
        redis_url=None,
        seedance_client=DummySeedanceClient(),
        redis_client=redis,
        task_scheduler=discard_background_task,
        poll_interval=0,
        timeout=30,
    )

    await manager.create_task("shared-task", "provider a prompt", provider_slug="provider-a")
    await manager.create_task("shared-task", "provider b prompt", provider_slug="provider-b")

    task_a = await manager.get_task("shared-task", provider_slug="provider-a")
    task_b = await manager.get_task("shared-task", provider_slug="provider-b")

    assert task_a.prompt == "provider a prompt"
    assert task_b.prompt == "provider b prompt"
    assert task_a.id == "shared-task"
    assert task_b.id == "shared-task"