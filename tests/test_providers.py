import importlib.util
import os
import sys
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
