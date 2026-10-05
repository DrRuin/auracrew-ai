"""App settings: secrets from .env, everything else from these defaults, any of them overridable by the environment."""

from functools import cache
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).parents[2]


class Settings(BaseSettings):
    """Every setting the app reads, with its default."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    openai_api_key: SecretStr
    typesafe_api_key: SecretStr
    mistral_api_key: SecretStr | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_user_email: str | None = None
    langfuse_user_password: SecretStr | None = None
    langfuse_host: str = "http://localhost:8000/langfuse"
    langfuse_project: str = "criteria-agent"
    environment: str = "local"
    model_id: str = "gpt-6-luna"
    judge_effort: str | None = "none"
    reasoning_effort: str | None = None
    typesafe_model: str = "jev-1.13.0"
    ocr_model: str = "mistral-ocr-latest"
    ocr_url: str = "https://api.mistral.ai/v1/ocr"
    converter_url: str = "http://127.0.0.1:3100"
    database_url: SecretStr = SecretStr("postgresql://postgres:postgres@127.0.0.1:5432/postgres")
    database_schema: str = "criteria"
    run_schema: str | None = None
    max_turns: int = 10
    parallel_calls: int = 16
    hedge_quantile: float = 0.95
    excerpt_words: int = 100
    render_scale: float = 2.0
    open_documents: int = 16
    request_timeout: float = 600.0
    connect_timeout: float = 10.0
    reset_enabled: bool = True
    journey_credentials: bool = True


@cache
def settings() -> Settings:
    """Load settings once on first use."""
    return Settings()


def client(**options: Any) -> httpx.AsyncClient:
    """An HTTP client with the configured timeouts."""
    config = settings()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(config.request_timeout, connect=config.connect_timeout),
        **options,
    )
