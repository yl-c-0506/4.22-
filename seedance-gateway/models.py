from enum import Enum
import re
from typing import Any, Optional
from pydantic import BaseModel, HttpUrl, field_validator, Field


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class OpenAIVideoRequest(BaseModel):
    """OpenAI 兼容的视频生成请求"""
    prompt: Optional[str] = None
    model: str = "seedance-v1"
    fast: Optional[bool] = None
    seed: Optional[int] = None
    image: Optional[str] = None
    ratio: Optional[str] = None
    duration: Optional[int] = None
    watermark: Optional[bool] = None
    last_image: Optional[str] = None
    resolution: Optional[str] = None
    web_search: Optional[bool] = None
    generate_audio: Optional[bool] = None
    reference_audios: Optional[list[str]] = None
    reference_images: Optional[list[str]] = None
    reference_videos: Optional[list[str]] = None
    return_last_frame: Optional[bool] = None


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None


class OpenAIChatCompletionsRequest(OpenAIVideoRequest):
    messages: list[ChatMessage]
    stream: Optional[bool] = None


class SeedanceTaskRequest(BaseModel):
    """Seedance API 任务请求"""
    prompt: Optional[str] = None
    fast: Optional[bool] = None
    seed: Optional[int] = None
    image: Optional[str] = None
    ratio: Optional[str] = None
    duration: Optional[int] = None
    watermark: Optional[bool] = None
    last_image: Optional[str] = None
    resolution: Optional[str] = None
    web_search: Optional[bool] = None
    generate_audio: Optional[bool] = None
    reference_audios: Optional[list[str]] = None
    reference_images: Optional[list[str]] = None
    reference_videos: Optional[list[str]] = None
    return_last_frame: Optional[bool] = None


class TaskStatusResponse(BaseModel):
    """任务状态响应"""
    id: str
    status: TaskStatus
    prompt: Optional[str] = None
    result_url: Optional[str] = None
    error: Optional[str] = None
    progress: int = 0
    created_at: Optional[float] = None


class ProviderConfigBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="显示名称")
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$", description="唯一标识符，只允许小写字母、数字和连字符")
    base_url: str = Field(..., min_length=1, max_length=256, description="上游基础URL")
    api_keys: list[str] = Field(..., min_length=1, description="API Key列表")
    provider_type: str = "seedance"
    enabled: bool = True
    is_default: bool = False

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        normalized_value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9-]+", normalized_value):
            raise ValueError("slug must contain only lowercase letters, numbers, and hyphens")
        return normalized_value

    @field_validator("name", "base_url")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("value must not be empty")
        return normalized_value

    @field_validator("api_keys")
    @classmethod
    def validate_api_keys(cls, values: list[str]) -> list[str]:
        normalized_values = [value.strip() for value in values if value.strip()]
        if not normalized_values:
            raise ValueError("api_keys must contain at least one non-empty key")
        return normalized_values


class ProviderCreateRequest(ProviderConfigBase):
    pass


class ProviderUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64, description="显示名称")
    base_url: Optional[str] = Field(None, min_length=1, max_length=256, description="上游基础URL")
    api_keys: Optional[list[str]] = Field(None, description="API Key列表")
    enabled: Optional[bool] = None

    @field_validator("name", "base_url")
    @classmethod
    def validate_non_empty(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("value must not be empty")
        return normalized_value

    @field_validator("api_keys")
    @classmethod
    def validate_api_keys(cls, values: Optional[list[str]]) -> Optional[list[str]]:
        if values is None:
            return values
        normalized_values = [value.strip() for value in values if value.strip()]
        if not normalized_values:
            raise ValueError("api_keys must contain at least one non-empty key")
        return normalized_values

class ProviderConfig(ProviderConfigBase):
    created_at: float
    updated_at: float


class ProviderSummaryResponse(BaseModel):
    name: str
    slug: str
    base_url: str
    provider_type: str = "seedance"
    enabled: bool = True
    is_default: bool = False
    api_key_count: int
    video_generation_url: str
    chat_completions_url: str
    task_status_url_template: str


class ProviderListResponse(BaseModel):
    items: list[ProviderSummaryResponse]
