"""LLM provider configuration and client construction.

The judge and verify stages should depend on this module instead of importing a
provider SDK directly. That keeps the Anthropic path available while making the
default provider easy to switch with CLI flags or environment variables.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LLMProvider(str, Enum):
    openai = "openai"
    anthropic = "anthropic"


DEFAULT_MODELS = {
    LLMProvider.openai: "gpt-5",
    LLMProvider.anthropic: "claude-sonnet-5",
}


class LLMConfig(BaseModel):
    provider: LLMProvider = LLMProvider.anthropic
    model: str | None = None
    judge_effort: str = "high"
    verify_effort: str = "low"
    enabled: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS[self.provider]


def config_from_env(
    *,
    provider: str | None = None,
    model: str | None = None,
    enabled: bool = True,
) -> LLMConfig:
    """Build LLM config from explicit values first, then environment defaults."""
    selected_provider = provider or os.getenv("PROD_TRACKER_LLM_PROVIDER") or "anthropic"
    selected_model = model or os.getenv("PROD_TRACKER_LLM_MODEL")
    return LLMConfig(provider=LLMProvider(selected_provider), model=selected_model, enabled=enabled)


def create_client(config: LLMConfig) -> Any:
    """Create a provider SDK client lazily.

    Lazy imports keep deterministic/dynamic-only runs from requiring either SDK.
    """
    if config.provider is LLMProvider.openai:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "OpenAI provider selected but the openai package is not installed. "
                "Install with `pip install 'prod-readiness-tracker[llm]'`."
            ) from exc
        return OpenAI()

    if config.provider is LLMProvider.anthropic:
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError(
                "Anthropic provider selected but the anthropic package is not installed. "
                "Install with `pip install 'prod-readiness-tracker[llm]'`."
            ) from exc
        return Anthropic()

    raise ValueError(f"Unsupported LLM provider: {config.provider}")
