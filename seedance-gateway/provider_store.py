import json
import time
from typing import Protocol, Set

from models import ProviderConfig, ProviderCreateRequest, ProviderListResponse, ProviderSummaryResponse, ProviderUpdateRequest


class ProviderStoreRedis(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None: ...

    async def setex(self, key: str, ttl: int, value: str) -> None: ...

    async def delete(self, key: str) -> int: ...

    async def sadd(self, key: str, value: str) -> int: ...

    async def smembers(self, key: str) -> Set[str]: ...
    
    async def srem(self, key: str, value: str) -> int: ...


class ProviderAlreadyExistsError(Exception):
    pass


class ProviderNotFoundError(Exception):
    pass


class ProviderStore:
    def __init__(
        self,
        redis_client: ProviderStoreRedis,
        base_public_url: str,
        provider_index_key: str = "providers:index",
        default_provider_key: str = "provider:default",
    ):
        self.redis = redis_client
        self.base_public_url = base_public_url.rstrip("/")
        self.provider_index_key = provider_index_key
        self.default_provider_key = default_provider_key

    def _provider_key(self, slug: str) -> str:
        return f"provider:{slug}"

    def _build_summary(self, provider: ProviderConfig) -> ProviderSummaryResponse:
        return ProviderSummaryResponse(
            name=provider.name,
            slug=provider.slug,
            base_url=provider.base_url,
            provider_type=provider.provider_type,
            enabled=provider.enabled,
            is_default=provider.is_default,
            api_key_count=len(provider.api_keys),
            video_generation_url=f"{self.base_public_url}/v1/providers/{provider.slug}/video/generations",
            chat_completions_url=f"{self.base_public_url}/v1/providers/{provider.slug}/chat/completions",
            task_status_url_template=f"{self.base_public_url}/v1/providers/{provider.slug}/tasks/{{task_id}}",
        )

    async def create_provider(self, request: ProviderCreateRequest) -> ProviderSummaryResponse:
        existing = await self.redis.get(self._provider_key(request.slug))
        if existing:
            raise ProviderAlreadyExistsError(request.slug)

        now = time.time()
        provider = ProviderConfig(
            **request.model_dump(),
            created_at=now,
            updated_at=now,
        )

        if provider.is_default:
            await self._clear_existing_default_provider()
            await self.redis.set(self.default_provider_key, provider.slug)

        await self.redis.set(self._provider_key(provider.slug), provider.model_dump_json())
        await self.redis.sadd(self.provider_index_key, provider.slug)
        return self._build_summary(provider)

    async def list_providers(self) -> ProviderListResponse:
        provider_slugs = sorted(await self.redis.smembers(self.provider_index_key))
        items: list[ProviderSummaryResponse] = []
        for provider_slug in provider_slugs:
            provider = await self.get_provider(provider_slug)
            items.append(self._build_summary(provider))
        return ProviderListResponse(items=items)

    async def get_provider(self, slug: str) -> ProviderConfig:
        payload = await self.redis.get(self._provider_key(slug))
        if not payload:
            raise ProviderNotFoundError(slug)
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return ProviderConfig(**json.loads(payload))
    
    async def update_provider(self, slug: str, request: ProviderUpdateRequest) -> ProviderSummaryResponse:
        provider = await self.get_provider(slug)
        update_data = request.model_dump(exclude_unset=True)
        if not update_data:
            return self._build_summary(provider)
        
        updated_provider = provider.model_copy(update={**update_data, "updated_at": time.time()})
        await self.redis.set(self._provider_key(slug), updated_provider.model_dump_json())
        return self._build_summary(updated_provider)

    async def delete_provider(self, slug: str) -> None:
        provider = await self.get_provider(slug)
        if provider.is_default:
            await self.redis.delete(self.default_provider_key)
        await self.redis.delete(self._provider_key(slug))
        await self.redis.srem(self.provider_index_key, slug)

    async def get_default_provider(self) -> ProviderConfig:
        default_slug = await self.redis.get(self.default_provider_key)
        if isinstance(default_slug, bytes):
            default_slug = default_slug.decode("utf-8")
        if default_slug:
            return await self.get_provider(default_slug)

        provider_list = await self.list_providers()
        if not provider_list.items:
            raise ProviderNotFoundError("default")
        return await self.get_provider(provider_list.items[0].slug)

    async def set_default_provider(self, slug: str) -> ProviderSummaryResponse:
        target_provider = await self.get_provider(slug)
        await self._clear_existing_default_provider()

        updated_provider = target_provider.model_copy(update={"is_default": True, "updated_at": time.time()})
        await self.redis.set(self._provider_key(slug), updated_provider.model_dump_json())
        await self.redis.set(self.default_provider_key, slug)
        return self._build_summary(updated_provider)

    async def _clear_existing_default_provider(self) -> None:
        current_default_slug = await self.redis.get(self.default_provider_key)
        if isinstance(current_default_slug, bytes):
            current_default_slug = current_default_slug.decode("utf-8")
        if not current_default_slug:
            return

        payload = await self.redis.get(self._provider_key(current_default_slug))
        if not payload:
            return
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")

        provider = ProviderConfig(**json.loads(payload))
        if not provider.is_default:
            return

        updated_provider = provider.model_copy(update={"is_default": False, "updated_at": time.time()})
        await self.redis.set(self._provider_key(current_default_slug), updated_provider.model_dump_json())
