import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import openai

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from providers.base import REQUEST_TIMEOUT_SECONDS, ToolCall, ToolResult, Turn
from providers.gemini import GeminiProvider
from providers.openai_compatible import OpenAICompatibleProvider

HAS_ANTHROPIC = importlib.util.find_spec("anthropic") is not None


class RequestTimeout(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"FAKE_KEY": "k"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_timeout_is_bounded(self):
        self.assertTrue(0 < REQUEST_TIMEOUT_SECONDS <= 300)

    def test_gemini_timeout_is_in_milliseconds(self):
        p = GeminiProvider("m", "FAKE_KEY")
        self.assertEqual(p._client._api_client._http_options.timeout, REQUEST_TIMEOUT_SECONDS * 1000)

    def test_openai_compatible_timeout_is_in_seconds(self):
        p = OpenAICompatibleProvider("m", "FAKE_KEY", "https://example.invalid/v1")
        self.assertEqual(p._client.timeout, REQUEST_TIMEOUT_SECONDS)

    @unittest.skipUnless(HAS_ANTHROPIC, "anthropic package not installed")
    def test_anthropic_timeout_is_in_seconds(self):
        from providers.anthropic import AnthropicProvider
        p = AnthropicProvider("m", "FAKE_KEY")
        self.assertEqual(p._client.timeout, REQUEST_TIMEOUT_SECONDS)


class EmptyCompletion(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"FAKE_KEY": "k"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = OpenAICompatibleProvider("some/model", "FAKE_KEY", "https://example.invalid/v1")
        sleeper = mock.patch("providers.openai_compatible.time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def generate_with(self, *responses):
        with mock.patch.object(self.provider._client.chat.completions, "create", side_effect=list(responses)) as create:
            try:
                return self.provider.generate([], [], "system")
            finally:
                self.calls = create.call_count

    def test_overloaded_provider_is_retried(self):
        overloaded = SimpleNamespace(choices=None, error={"message": "Service temporarily overloaded", "code": 503})
        message = SimpleNamespace(content="hi", tool_calls=None)
        result = self.generate_with(overloaded, overloaded, SimpleNamespace(choices=[SimpleNamespace(message=message)]))
        self.assertEqual((result.text, self.calls), ("hi", 3))

    def test_overload_that_never_clears_still_raises(self):
        overloaded = SimpleNamespace(choices=None, error={"message": "Service temporarily overloaded", "code": 503})
        with self.assertRaises(RuntimeError) as ctx:
            self.generate_with(*[overloaded] * 5)
        self.assertIn("overloaded", str(ctx.exception))
        self.assertEqual(self.calls, 5)

    def test_server_error_is_retried(self):
        request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
        error = openai.InternalServerError("Provider returned error", response=httpx.Response(502, request=request),
                                           body=None)
        message = SimpleNamespace(content="hi", tool_calls=None)
        result = self.generate_with(error, SimpleNamespace(choices=[SimpleNamespace(message=message)]))
        self.assertEqual((result.text, self.calls), ("hi", 2))

    def test_permanent_error_is_not_retried(self):
        with self.assertRaises(RuntimeError):
            self.generate_with(SimpleNamespace(choices=None, error={"message": "bad request", "code": 400}))
        self.assertEqual(self.calls, 1)

    def test_null_choices_raise_a_readable_error(self):
        upstream = {"message": "Provider returned error", "code": 502}
        with self.assertRaises(RuntimeError) as ctx:
            self.generate_with(*[SimpleNamespace(choices=None, error=upstream)] * 5)
        self.assertIn("some/model", str(ctx.exception))
        self.assertIn("Provider returned error", str(ctx.exception))

    def test_empty_choices_without_an_error_field(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.generate_with(SimpleNamespace(choices=[]))
        self.assertIn("no choices", str(ctx.exception))

    def test_normal_completion_still_parses(self):
        message = SimpleNamespace(content="hi", tool_calls=None)
        result = self.generate_with(SimpleNamespace(choices=[SimpleNamespace(message=message)]))
        self.assertEqual((result.text, result.tool_calls), ("hi", []))


def anthropic_message(*content, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=list(content), stop_reason=stop_reason, stop_details=stop_details)


@unittest.skipUnless(HAS_ANTHROPIC, "anthropic package not installed")
class AnthropicAdapter(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"FAKE_KEY": "k"})
        patcher.start()
        self.addCleanup(patcher.stop)
        from providers.anthropic import AnthropicProvider
        self.provider = AnthropicProvider("claude-test", "FAKE_KEY")
        self.sent = []

    def generate_with(self, message, history=None):
        def stream(**kwargs):
            self.sent.append(kwargs)
            cm = mock.MagicMock()
            cm.__enter__.return_value.get_final_message.return_value = message
            return cm
        with mock.patch.object(self.provider._client.messages, "stream", side_effect=stream):
            return self.provider.generate(history or [Turn(role="user", text="hi")], [], "system")

    def test_reply_is_parsed_and_its_blocks_kept(self):
        thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
        text = SimpleNamespace(type="text", text="Reading it.")
        call = SimpleNamespace(type="tool_use", id="tu_1", name="read_file", input={"path": "a.py"})
        result = self.generate_with(anthropic_message(thinking, text, call))
        self.assertEqual(result.text, "Reading it.")
        self.assertEqual(result.tool_calls, [ToolCall(id="tu_1", name="read_file", args={"path": "a.py"})])
        self.assertEqual(result.raw, [thinking, text, call])

    def test_raw_blocks_go_back_unchanged(self):
        blocks = [SimpleNamespace(type="thinking", thinking="", signature="sig")]
        history = [
            Turn(role="user", text="hi"),
            Turn(role="assistant", text="ignored", tool_calls=[ToolCall("tu_1", "x", {})], raw=blocks),
            Turn(role="user", tool_results=[ToolResult("tu_1", "x", "ok")]),
        ]
        self.generate_with(anthropic_message(), history)
        self.assertIs(self.sent[0]["messages"][1]["content"], blocks)

    def test_turn_without_raw_is_rebuilt(self):
        history = [
            Turn(role="user", text="hi"),
            Turn(role="assistant", text="calling", tool_calls=[ToolCall("tu_1", "x", {"a": 1})]),
            Turn(role="user", tool_results=[ToolResult("tu_1", "x", "ok")]),
        ]
        self.generate_with(anthropic_message(), history)
        self.assertEqual(self.sent[0]["messages"][1]["content"], [
            {"type": "text", "text": "calling"},
            {"type": "tool_use", "id": "tu_1", "name": "x", "input": {"a": 1}},
        ])

    def test_refusal_raises(self):
        details = SimpleNamespace(category="cyber")
        with self.assertRaises(RuntimeError) as ctx:
            self.generate_with(anthropic_message(stop_reason="refusal", stop_details=details))
        self.assertIn("cyber", str(ctx.exception))

    def test_leaves_room_for_thinking(self):
        self.generate_with(anthropic_message())
        self.assertGreaterEqual(self.sent[0]["max_tokens"], 16000)


if __name__ == "__main__":
    unittest.main()
