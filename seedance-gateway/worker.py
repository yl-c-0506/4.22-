import asyncio
import logging
import os

from dotenv import load_dotenv

from provider_store import ProviderNotFoundError, ProviderStore
from seedance_client import SeedanceClient
from task_manager import TaskManager

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seedance-worker")


def load_worker_config() -> tuple[list[str], str, str]:
    required_env = {
        "SEEDANCE_API_KEYS": os.getenv("SEEDANCE_API_KEYS"),
        "SEEDANCE_BASE_URL": os.getenv("SEEDANCE_BASE_URL"),
        "REDIS_URL": os.getenv("REDIS_URL"),
    }
    missing = [name for name, value in required_env.items() if not value]
    if missing:
        missing_names = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {missing_names}")

    keys = [key.strip() for key in required_env["SEEDANCE_API_KEYS"].split(",") if key.strip()]
    if not keys:
        raise RuntimeError("SEEDANCE_API_KEYS contains no valid keys")

    return keys, required_env["SEEDANCE_BASE_URL"], required_env["REDIS_URL"]


async def main() -> None:
    keys, base_url, redis_url = load_worker_config()
    pop_timeout = int(os.getenv("TASK_QUEUE_POP_TIMEOUT", 5))
    restart_delay_seconds = int(os.getenv("WORKER_RESTART_DELAY", 3))
    public_url = os.getenv("GATEWAY_PUBLIC_URL", "http://localhost:8001")

    client = SeedanceClient(keys, base_url)
    manager = TaskManager(
        redis_url,
        client,
        execution_mode="queue",
    )

    provider_store = ProviderStore(manager.redis, public_url)

    async def resolve_client(provider_slug: str | None) -> SeedanceClient:
        if not provider_slug or provider_slug == "default":
            return client

        try:
            provider = await provider_store.get_provider(provider_slug)
        except ProviderNotFoundError:
            logger.warning(f"Provider '{provider_slug}' not found, fallback to default")
            return client

        if not provider.enabled:
            logger.warning(f"Provider '{provider_slug}' is disabled, fallback to default")
            return client

        return SeedanceClient(provider.api_keys, provider.base_url)

    manager.client_resolver = resolve_client

    logger.info("Seedance worker started")
    while True:
        try:
            await manager.run_worker(pop_timeout=pop_timeout)
        except asyncio.CancelledError:
            logger.info("Seedance worker cancelled")
            raise
        except Exception:
            logger.exception("Worker loop failed; restarting after backoff")
            await asyncio.sleep(restart_delay_seconds)


if __name__ == "__main__":
    asyncio.run(main())
