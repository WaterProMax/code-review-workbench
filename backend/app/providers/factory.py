"""Provider selection: the single place a real model client is constructed."""

from __future__ import annotations

from app.providers.base import LLMClient
from app.providers.deepseek import DeepSeekClient
from app.settings import Settings


def build_llm_client(settings: Settings) -> LLMClient:
    """Return the configured real model client.

    Raises a clear configuration error when credentials are missing so that new
    model tasks fail loudly instead of silently using a fake model (§P00.5).
    """
    settings.require_model_credentials()
    if settings.model_provider != "deepseek":
        raise ValueError(f"unsupported model provider {settings.model_provider!r}")
    return DeepSeekClient(
        api_key=settings.model_api_key or "",
        base_url=settings.model_base_url,
        model=settings.model_name,
        timeout_seconds=settings.model_timeout_seconds,
        max_retries=settings.max_model_retries,
        temperature=settings.model_temperature,
    )
