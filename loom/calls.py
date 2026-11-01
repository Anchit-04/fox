import hashlib
import json
import time

from events import emit
from providers import ProviderResponse, Turn
from providers.config import call_cost


def _digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _turn_digest(turn: Turn) -> str:
    return _digest({
        "role": turn.role,
        "text": turn.text,
        "tool_calls": [[tc.id, tc.name, tc.args] for tc in turn.tool_calls],
        "tool_results": [[tr.call_id, tr.name, tr.content] for tr in turn.tool_results],
        "raw": turn.raw,
    })


def prefix_digest(turns: list[Turn], tools: list[dict], system_prompt: str) -> dict:
    return {"tools": _digest(tools), "system": _digest(system_prompt),
            "messages": [_turn_digest(turn) for turn in turns]}


def timed_generate(provider, turns: list[Turn], tools: list[dict], system_prompt: str, *, model_key: str,
                   task_id: str | None, event_sink) -> ProviderResponse:
    prefix = prefix_digest(turns, tools, system_prompt)
    started = time.perf_counter()
    response = provider.generate(turns, tools, system_prompt)
    duration_ms = round((time.perf_counter() - started) * 1000)
    usage = response.usage
    cost, cost_source = call_cost(model_key, usage)
    emit(event_sink, "model_called", {
        "task_id": task_id, "model_key": model_key, "duration_ms": duration_ms,
        "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens, "cache_write_tokens": usage.cache_write_tokens,
        "cost": cost, "cost_source": cost_source, "prefix": prefix,
    })
    return response
