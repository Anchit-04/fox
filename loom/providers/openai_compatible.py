"""
Adapter for any OpenAI-compatible chat-completions API. DeepSeek and
Moonshot/Kimi both implement the same wire format OpenAI does (tools /
tool_calls shape included), so one adapter covers all three — point it at a
different model_id + base_url in providers/config.py and it works
unchanged. That's the bulk of "support any LLM": most non-Gemini,
non-Anthropic providers speak this dialect.
"""

import json
import time

from openai import InternalServerError, OpenAI, RateLimitError

from .base import REQUEST_TIMEOUT_SECONDS, Provider, ProviderResponse, ToolCall, Turn, Usage

MAX_RETRIES = 5


class OpenAICompatibleProvider(Provider):
    def __init__(self, model_id: str, env_key: str, base_url: str | None = None):
        super().__init__(model_id, env_key, base_url)
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=REQUEST_TIMEOUT_SECONDS)

    def generate(self, history: list[Turn], tools: list[dict], system_prompt: str) -> ProviderResponse:
        messages = [{"role": "system", "content": system_prompt}] + _to_openai_messages(history)
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]
        extra = {"tools": oai_tools} if oai_tools else {}
        response = self._create_with_retry(model=self.model_id, messages=messages, **extra)

        if not response.choices:
            error = getattr(response, "error", None) or "no choices in the response"
            raise RuntimeError(f"{self.model_id} returned no completion: {error}")
        choice = response.choices[0].message
        tool_calls = []
        for tc in (choice.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                # Cheaper models can return truncated JSON here — still owe a
                # ToolResult for this call_id, so flag it via `raw` instead of dropping it.
                tool_calls.append(ToolCall(
                    id=tc.id, name=tc.function.name, args={},
                    raw={"args_parse_error": str(e), "raw_arguments": tc.function.arguments},
                ))
                continue
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
        return ProviderResponse(text=choice.content, tool_calls=tool_calls,
                                usage=_usage(getattr(response, "usage", None)))

    def _create_with_retry(self, **kwargs):
        for attempt in range(MAX_RETRIES):
            wait = 2 ** attempt
            last = attempt == MAX_RETRIES - 1
            try:
                response = self._client.chat.completions.create(**kwargs)
            except (RateLimitError, InternalServerError) as e:
                if last:
                    raise
                reason = "Rate limited" if isinstance(e, RateLimitError) else f"Server error ({e.status_code})"
                print(f"\033[91m{reason}, waiting {wait}s (attempt {attempt + 1}/{MAX_RETRIES})...\033[0m")
                time.sleep(wait)
                continue
            if response.choices or not _is_transient(response) or last:
                return response
            print(f"\033[91mProvider overloaded, waiting {wait}s (attempt {attempt + 1}/{MAX_RETRIES})...\033[0m")
            time.sleep(wait)


TRANSIENT_CODES = {429, 502, 503}


def _is_transient(response) -> bool:
    error = getattr(response, "error", None)
    return isinstance(error, dict) and error.get("code") in TRANSIENT_CODES


def _usage(usage) -> Usage:
    if usage is None:
        return Usage()
    prompt = usage.prompt_tokens or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", None) or getattr(usage, "prompt_cache_hit_tokens", None)
              or 0)
    written = getattr(details, "cache_write_tokens", None) or 0
    cost = getattr(usage, "cost", None)
    return Usage(input_tokens=prompt - cached - written, output_tokens=usage.completion_tokens or 0,
                 cache_read_tokens=cached, cache_write_tokens=written,
                 reported_cost=float(cost) if isinstance(cost, (int, float)) else None)


def _to_openai_messages(history: list) -> list:
    messages = []
    for turn in history:
        if turn.role == "user" and turn.tool_results:
            for tr in turn.tool_results:
                messages.append({"role": "tool", "tool_call_id": tr.call_id, "content": tr.content})
            shown = [(tr.name, image) for tr in turn.tool_results for image in tr.images]
            if shown:
                names = ", ".join(dict.fromkeys(name for name, _ in shown))
                messages.append({"role": "user", "content": [{"type": "text", "text": f"Images returned by {names}:"}]
                                 + [_image_part(image) for _, image in shown]})
        elif turn.role == "assistant":
            msg = {"role": "assistant", "content": turn.text}
            if turn.tool_calls:
                msg["tool_calls"] = [
                    {
                        "type": "function",
                        "id": tc.id,
                        "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                    }
                    for tc in turn.tool_calls
                ]
            messages.append(msg)
        elif turn.images:
            messages.append({"role": "user", "content": [{"type": "text", "text": turn.text or ""}]
                             + [_image_part(image) for image in turn.images]})
        else:
            messages.append({"role": "user", "content": turn.text or ""})
    return messages


def _image_part(image) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{image.data}"}}
