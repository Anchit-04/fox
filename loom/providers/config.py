"""
Model registry — the one file you touch to add a model. Everything upstream
refers to models only by the short key below, never SDK type or raw model id.

To add one: pick a key, point it at a provider module (a key of
PROVIDER_CLASSES) + real model id, name the env var holding its key
(actual key goes in .env, never here). Not
Gemini/Anthropic? It's almost certainly OpenAI-compatible. "tier" is pure
routing metadata, not a constructor arg — must be popped in get_provider()
below or every call breaks with an unexpected-kwarg TypeError.
"""

from importlib import import_module

from .base import Usage

PROVIDER_CLASSES = {
    "anthropic": "AnthropicProvider",
    "gemini": "GeminiProvider",
    "openai_compatible": "OpenAICompatibleProvider",
}

FREE = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}

MODEL_REGISTRY = {
    "claude-opus": {
        "provider": "anthropic",
        "model_id": "claude-opus-5-5",
        "price": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
        "env_key": "ANTHROPIC_API_KEY",
        "tier": "strong",
        "vision": True,
    },
    "claude-haiku": {
        "provider": "anthropic",
        "model_id": "claude-haiku-5-5",
        "price": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125,
                  "long_context": {"over": 100_000, "input": 0.50, "output": 2.50, "cache_read": 0.05,
                                   "cache_write": 0.625}},
        "env_key": "ANTHROPIC_API_KEY",
        "tier": "cheap",
        "vision": True,
    },
    "gemini-lite": {
        "provider": "gemini",
        "model_id": "gemini-3.1-flash-lite",
        "price": FREE,
        "env_key": "GEMINI_API_KEY",
        "tier": "cheap",
        "vision": True,
    },
    "gemini-flash": {
        "provider": "gemini",
        "model_id": "gemini-3.8-flash",  # was 3.6-flash — that id started returning 503 "high demand"; 3.8 is current-gen and on the same key
        "price": FREE,
        "env_key": "GEMINI_API_KEY",
        "tier": "cheap",
        "vision": True,
    },
    "deepseek-chat": {
        "provider": "openai_compatible",
        "model_id": "deepseek-chat",
        "price": None,
        "env_key": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "tier": "cheap",
        "vision": False,
    },
    "kimi-k2": {
        "provider": "openai_compatible",
        "model_id": "kimi-k2-0711-preview",
        "price": None,
        "env_key": "MOONSHOT_API_KEY",
        "base_url": "https://api.moonshot.ai/v1",
        "tier": "cheap",
        "vision": False,
    },
    # OpenRouter — one key, many models. These point at :free variants
    # (rate-limited: ~20 req/min, ~50 req/day until $10 lifetime spend).
    # The :free roster churns — models get pulled without notice, and a
    # pulled id fails at call time, not at startup, since vault.is_present()
    # only checks the env var. Re-verify with GET /api/v1/models, filtering
    # pricing==0 AND 'tools' in supported_parameters. Free-tier models that
    # merely *accept* a tools array often won't actually emit tool calls —
    # both entries below were confirmed to emit a real one, not just chat.
    "north-code": {
        "provider": "openai_compatible",
        "model_id": "cohere/north-mini-code:free",
        "price": FREE,
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "tier": "cheap",
        "vision": False,
    },
    # Fastest verified free model — ~1.7s to first tool call, which is what
    # makes a live agent loop feel responsive rather than hung.
    "dots-note": {
        "provider": "openai_compatible",
        "model_id": "dots-studio/dots-3-note-preview:free",
        "price": FREE,
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "tier": "cheap",
        "vision": True,
    },
    # 1M context — the one to reach for when a task reads a lot of files.
    # ~6.5s/call. Its 550b sibling is nemotron-ultra below: it tool-calls but was ~39s/call when measured.
    "nemotron-lightning": {
        "provider": "openai_compatible",
        "model_id": "nvidia/nemotron-3.5-lightning:free",
        "price": FREE,
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "tier": "cheap",
        "vision": False,
    },
    "nemotron-ultra": {
        "provider": "openai_compatible",
        "model_id": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "price": FREE,
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "tier": "strong",
        "vision": False,
    },
    "nemotron-super": {
        "provider": "openai_compatible",
        "model_id": "nvidia/nemotron-3.5-super-vl:free",
        "price": FREE,
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "tier": "strong",
        "vision": True,
    },
}


def provider_class(module: str) -> type:
    return getattr(import_module(f"{__package__}.{module}"), PROVIDER_CLASSES[module])


def get_provider(key: str):
    """Instantiate the Provider for a registry key. Raises if the key is
    unknown, or if that provider's API key env var isn't set."""
    if key not in MODEL_REGISTRY:
        raise ValueError(f"Unknown model key {key!r}. Known: {sorted(MODEL_REGISTRY)}")
    cfg = dict(MODEL_REGISTRY[key])
    provider_cls = provider_class(cfg.pop("provider"))
    model_id = cfg.pop("model_id")
    cfg.pop("tier", None)  # routing/vault metadata, not a Provider constructor arg
    cfg.pop("price", None)
    cfg.pop("vision", None)
    return provider_cls(model_id=model_id, **cfg)


def has_vision(key: str) -> bool:
    return bool(MODEL_REGISTRY.get(key, {}).get("vision"))


def get_tier(key: str) -> str:
    """The routing tier for a registry key."""
    if key not in MODEL_REGISTRY:
        raise ValueError(f"Unknown model key {key!r}. Known: {sorted(MODEL_REGISTRY)}")
    return MODEL_REGISTRY[key]["tier"]


def cost_of(key: str, usage: Usage) -> float | None:
    price = MODEL_REGISTRY.get(key, {}).get("price")
    if price is None:
        return None
    long_context = price.get("long_context")
    if long_context and usage.prompt_tokens > long_context["over"]:
        price = long_context
    return (usage.input_tokens * price["input"] + usage.output_tokens * price["output"]
            + usage.cache_read_tokens * price["cache_read"]
            + usage.cache_write_tokens * price["cache_write"]) / 1_000_000


def call_cost(key: str, usage: Usage) -> tuple[float | None, str | None]:
    if usage.reported_cost is not None:
        return usage.reported_cost, "provider"
    cost = cost_of(key, usage)
    return cost, None if cost is None else "catalog"
