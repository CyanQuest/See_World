from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

# Videos processed per batch, and in total per task (failed attempts count).
BATCH_VIDEOS = 5
TASK_VIDEOS = 15

class Preferences(BaseModel):
    destination: str = Field(min_length=1, max_length=100)
    days: int = Field(3, ge=1, le=30)
    date: str = Field('', max_length=100)
    origin: str = Field('', max_length=100)
    people: int = Field(1, ge=1, le=20)
    budget: str = Field('', max_length=100)
    include_arrival: bool = False
    interests: list[str] = Field(default_factory=list, max_length=10)
    pace: Literal['轻松', '适中', '紧凑'] = '适中'
    notes: str = Field('', max_length=2000)
    selection: Literal['auto', 'manual'] = 'auto'
    transcription: Literal['subtitle', 'moss'] = 'subtitle'
    links: list[str] = Field(default_factory=list, max_length=30)

    @field_validator('destination', mode='before')
    @classmethod
    def strip_destination(cls, value):
        return value.strip() if isinstance(value, str) else value

class Settings(BaseModel):
    moss_base: str = 'https://api.mosi.cn'
    moss_key: str = ''
    llm_provider: str = '自定义服务'
    llm_base: str = ''
    llm_model: str = ''
    llm_key: str = ''
    llm_protocol: Literal['chat_completions'] = 'chat_completions'
    search_count: int = Field(10, ge=1, le=10)
    search_groups: int = Field(4, ge=1, le=6)
    max_videos: int = Field(5, ge=1, le=15)
    max_minutes: int = Field(60, ge=1, le=180)
    max_video_minutes: int = Field(30, ge=1, le=90)
    recent_months: int = Field(24, ge=1, le=120)
    chunk_seconds: int = Field(300, ge=30, le=600)
    overlap_seconds: int = Field(2, ge=0, le=5)
    chunk_mb: int = Field(20, ge=1, le=20)
    transcribe_concurrency: int = Field(2, ge=1, le=4)
    subtitle_chars: int = Field(100, ge=20, le=1000)
    subtitle_coverage: float = Field(.5, ge=.1, le=1)
    moss_timeout: int = Field(300, ge=30, le=900)
    llm_timeout: int = Field(180, ge=30, le=900)
    context_budget: int = Field(24000, ge=6000, le=100000)
    output_tokens: int = Field(6000, ge=1000, le=16384)
    max_requests: int = Field(120, ge=5, le=1000)
    max_llm_tokens: int = Field(250000, ge=10000, le=2000000)
    max_audio_minutes: int = Field(90, ge=1, le=600)
    revision_rounds: int = Field(2, ge=0, le=2)
    retain_media: bool = False
    moss_price_minute: float | None = Field(None, ge=0)
    llm_price_million: float | None = Field(None, ge=0)
    cookies: str = Field('', max_length=1000000)

    @model_validator(mode='after')
    def compatible_limits(self):
        if self.context_budget < self.output_tokens + 5000:
            raise ValueError('上下文预算至少应比输出上限大 5000')
        if self.overlap_seconds >= self.chunk_seconds:
            raise ValueError('重叠时长必须短于分片时长')
        return self
