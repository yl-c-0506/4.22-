import os
import httpx
import logging
from typing import Optional
from models import SeedanceTaskRequest, TaskStatus

logger = logging.getLogger("seedance-client")
SUBMIT_PATH = "/v3/async/seedance-2.0"
TASK_RESULT_PATH = "/v3/async/task-result"
STATUS_MAP = {
    "TASK_STATUS_QUEUED": TaskStatus.QUEUED,
    "TASK_STATUS_PROCESSING": TaskStatus.PROCESSING,
    "TASK_STATUS_SUCCEED": TaskStatus.SUCCESS,
    "TASK_STATUS_FAILED": TaskStatus.FAILED,
}

class SeedanceClient:
    def __init__(
        self,
        api_keys: list[str],
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_keys = api_keys
        self.base_url = base_url
        self.transport = transport
        self.current_key_idx = 0
        self.failed_keys: set[str] = set()

    def _build_client(self, timeout: float) -> httpx.AsyncClient:
        client_options: dict[str, object] = {"timeout": timeout}
        if self.transport is not None:
            client_options["transport"] = self.transport
        return httpx.AsyncClient(**client_options)

    @staticmethod
    def _dump_request(req: SeedanceTaskRequest) -> dict[str, object]:
        if hasattr(req, "model_dump"):
            return req.model_dump(exclude_none=True)
        return req.dict(exclude_none=True)

    @staticmethod
    def _extract_result_url(data: dict[str, object]) -> str | None:
        videos = data.get("videos") or []
        if videos:
            return videos[0].get("video_url")

        images = data.get("images") or []
        if images:
            return images[0].get("image_url")

        audios = data.get("audios") or []
        if audios:
            return audios[0].get("audio_url")

        return None

    def _get_api_key(self) -> Optional[str]:
        """获取下一个可用的 Key，带简单熔断逻辑"""
        for _ in range(len(self.api_keys)):
            key = self.api_keys[self.current_key_idx].strip()
            self.current_key_idx = (self.current_key_idx + 1) % len(self.api_keys)
            
            if key not in self.failed_keys:
                return key
        return None

    def _mark_key_failed(self, key: str):
        """标记 Key 为失败（熔断）"""
        logger.warning(f"Marking key as failed: ****{key[-4:]}")
        self.failed_keys.add(key)

    async def submit_task(self, req: SeedanceTaskRequest) -> tuple[Optional[str], Optional[str]]:
        """提交任务，返回 (task_id, error)"""
        last_error: Optional[str] = None
        request_payload = self._dump_request(req)

        for _ in range(len(self.api_keys)):
            key = self._get_api_key()
            if not key:
                break

            headers = {"Authorization": f"Bearer {key}"}
            try:
                async with self._build_client(timeout=30.0) as client:
                    # 假设 Seedance 提交任务的接口是 /tasks
                    resp = await client.post(
                        f"{self.base_url}{SUBMIT_PATH}",
                        json=request_payload,
                        headers=headers
                    )
                    if resp.status_code == 401:
                        self._mark_key_failed(key)
                        last_error = f"Key error {resp.status_code}"
                        continue
                    if resp.status_code == 429:
                        last_error = f"Key error {resp.status_code}"
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    task_id = data.get("task_id")
                    if not task_id:
                        logger.error("Submit task succeeded without task_id in response")
                        return None, "Seedance response missing task_id"

                    return task_id, None
            except Exception as e:
                logger.error(f"Submit task failed: {str(e)}")
                return None, str(e)

        return None, last_error or "No available API keys"

    async def poll_task(
        self,
        task_id: str,
    ) -> tuple[Optional[TaskStatus], Optional[str], int, Optional[str]]:
        """轮询任务状态，返回 (status, result_url, progress, error)"""
        last_error: Optional[str] = None

        for _ in range(len(self.api_keys)):
            key = self._get_api_key()
            if not key:
                break

            headers = {"Authorization": f"Bearer {key}"}
            try:
                async with self._build_client(timeout=10.0) as client:
                    resp = await client.get(
                        f"{self.base_url}{TASK_RESULT_PATH}",
                        headers=headers,
                        params={"task_id": task_id},
                    )
                    if resp.status_code == 401:
                        self._mark_key_failed(key)
                        last_error = f"Key error {resp.status_code}"
                        continue
                    if resp.status_code == 429:
                        last_error = f"Key error {resp.status_code}"
                        continue

                    resp.raise_for_status()
                    data = resp.json()

                    task = data.get("task") or {}
                    status = STATUS_MAP.get(task.get("status"), TaskStatus.QUEUED)
                    progress = task.get("progress_percent") or 0
                    result_url = self._extract_result_url(data)
                    error = task.get("reason") if status == TaskStatus.FAILED else None
                    return status, result_url, progress, error
            except Exception as e:
                return None, None, 0, str(e)

        return None, None, 0, last_error or "No available keys"
