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


def is_available(config: LLMConfig | None = None, client: Any | None = None) -> bool:
    """Check if the configured LLM provider has credentials or a valid client."""
    if client is not None:
        return True
    cfg = config or config_from_env()
    if not cfg.enabled:
        return False
    if cfg.provider is LLMProvider.anthropic:
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    if cfg.provider is LLMProvider.openai:
        return bool(os.getenv("OPENAI_API_KEY"))
    return False


def generate_structured(
    config: LLMConfig,
    *,
    system: str,
    user: str,
    response_model: type[BaseModel],
    client: Any | None = None,
) -> Any:
    """Request structured output from the configured LLM provider adhering to response_model."""
    cli = client or create_client(config)

    # Allow custom/mock client to implement direct structured generation
    if hasattr(cli, "generate_structured"):
        return cli.generate_structured(
            config=config,
            system=system,
            user=user,
            response_model=response_model,
        )

    if config.provider is LLMProvider.anthropic:
        return _generate_anthropic(cli, config, system, user, response_model)

    if config.provider is LLMProvider.openai:
        return _generate_openai(cli, config, system, user, response_model)

    raise ValueError(f"Unsupported LLM provider: {config.provider}")


def _generate_anthropic(
    client: Any,
    config: LLMConfig,
    system: str,
    user: str,
    response_model: type[BaseModel],
) -> Any:
    tool_name = "structured_output"
    tool_schema = {
        "name": tool_name,
        "description": f"Output structured data adhering to {response_model.__name__}.",
        "input_schema": response_model.model_json_schema(),
    }
    response = client.messages.create(
        model=config.resolved_model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": tool_name},
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return response_model.model_validate(block.input)
    raise ValueError("Anthropic provider did not return structured tool call")


def _generate_openai(
    client: Any,
    config: LLMConfig,
    system: str,
    user: str,
    response_model: type[BaseModel],
) -> Any:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if hasattr(client, "beta") and hasattr(client.beta, "chat") and hasattr(client.beta.chat, "completions"):
        completion = client.beta.chat.completions.parse(
            model=config.resolved_model,
            messages=messages,
            response_format=response_model,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is not None:
            return parsed

    # Fallback to chat.completions.create with tool calling
    tool_name = "structured_output"
    tool_schema = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": f"Output structured data adhering to {response_model.__name__}.",
            "parameters": response_model.model_json_schema(),
        },
    }
    response = client.chat.completions.create(
        model=config.resolved_model,
        messages=messages,
        tools=[tool_schema],
        tool_choice={"type": "function", "function": {"name": tool_name}},
    )
    choice = response.choices[0]
    if choice.message.tool_calls:
        import json
        args = json.loads(choice.message.tool_calls[0].function.arguments)
        return response_model.model_validate(args)
    raise ValueError("OpenAI provider did not return structured tool call")
