import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from providers.base import REQUEST_TIMEOUT_SECONDS
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

    def generate_with(self, response):
        with mock.patch.object(self.provider._client.chat.completions, "create", return_value=response):
            return self.provider.generate([], [], "system")

    def test_null_choices_raise_a_readable_error(self):
        upstream = {"message": "Provider returned error", "code": 502}
        with self.assertRaises(RuntimeError) as ctx:
            self.generate_with(SimpleNamespace(choices=None, error=upstream))
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


if __name__ == "__main__":
    unittest.main()
