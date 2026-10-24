"""
Anthropic (Claude) adapter. Optional dependency — install with:
    pip install anthropic

Import is deferred to __init__ so the rest of the provider package (and
models that don't need Claude) still work without this package installed.
"""

import time

from .base import REQUEST_TIMEOUT_SECONDS, Provider, ProviderResponse, ToolCall, Turn

MAX_RETRIES = 5
MAX_TOKENS = 16000


class AnthropicProvider(Provider):
    def __init__(self, model_id: str, env_key: str, base_url: str | None = None):
        super().__init__(model_id, env_key, base_url)
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "AnthropicProvider needs the `anthropic` package: pip install anthropic"
            ) from e
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=self.api_key, base_url=self.base_url, timeout=REQUEST_TIMEOUT_SECONDS
        )

    def generate(self, history: list[Turn], tools: list[dict], system_prompt: str) -> ProviderResponse:
        messages = _to_anthropic_messages(history)
        anthropic_tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]
        response = self._create_with_retry(
            model=self.model_id,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=messages,
            tools=anthropic_tools,
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise RuntimeError(f"{self.model_id} refused the request (category: {category})")

        text = "".join(b.text for b in response.content if b.type == "text") or None
        tool_calls = [
            ToolCall(id=b.id, name=b.name, args=b.input)
            for b in response.content if b.type == "tool_use"
        ]
        return ProviderResponse(text=text, tool_calls=tool_calls, raw=list(response.content))

    def _create_with_retry(self, **kwargs):
        a = self._anthropic
        transient = (a.RateLimitError, a.OverloadedError, a.ServiceUnavailableError, a.InternalServerError)
        for attempt in range(MAX_RETRIES):
            try:
                with self._client.messages.stream(**kwargs) as stream:
                    return stream.get_final_message()
            except transient as e:
                wait = 2 ** attempt
                if attempt == MAX_RETRIES - 1:
                    raise
                reason = "Rate limited" if isinstance(e, a.RateLimitError) else f"Server error ({e.status_code})"
                print(f"\033[91m{reason}, waiting {wait}s (attempt {attempt + 1}/{MAX_RETRIES})...\033[0m")
                time.sleep(wait)


def _to_anthropic_messages(history: list) -> list:
    messages = []
    for turn in history:
        if turn.role == "user" and turn.tool_results:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tr.call_id, "content": tr.content}
                    for tr in turn.tool_results
                ],
            })
        elif turn.role == "assistant" and turn.raw is not None:
            messages.append({"role": "assistant", "content": turn.raw})
        elif turn.role == "assistant":
            content = []
            if turn.text:
                content.append({"type": "text", "text": turn.text})
            for tc in turn.tool_calls:
                content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": "user", "content": turn.text or ""})
    return messages
