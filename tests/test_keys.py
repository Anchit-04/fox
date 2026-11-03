import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import calls
from config import vault


class FreshKeys(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.env = self.home / ".env"
        for patcher in (mock.patch.object(vault, "FOX_HOME", self.home),
                        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "old-value"}),
                        mock.patch.dict(vault._validated_cache, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_key_written_to_env_by_another_process_wins_over_the_old_environment(self):
        self.env.write_text("OPENROUTER_API_KEY=new-value\n", encoding="utf-8")
        self.assertEqual(vault.get_key("nemotron-super"), "new-value")

    def test_without_an_env_entry_the_environment_is_used(self):
        self.assertEqual(vault.get_key("nemotron-super"), "old-value")
        self.assertTrue(vault.is_present("nemotron-super"))

    def test_reload_updates_the_environment_and_forgets_validation(self):
        vault._validated_cache["nemotron-super"] = False
        self.env.write_text("OPENROUTER_API_KEY=new-value\n", encoding="utf-8")
        vault.reload()
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "new-value")
        self.assertEqual(vault._validated_cache, {})

    def test_remember_keeps_only_known_models(self):
        vault.remember({"nemotron-super": True, "no-such-model": True})
        self.assertEqual(vault._validated_cache, {"nemotron-super": True})


class KeyedProvider:
    def __init__(self, api_key):
        self.api_key = api_key


class CurrentProvider(unittest.TestCase):
    def test_a_provider_built_with_an_old_key_is_rebuilt(self):
        rebuilt = KeyedProvider("new-value")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "new-value"}), \
                mock.patch("calls.get_provider", return_value=rebuilt) as get:
            self.assertIs(calls.current_provider(KeyedProvider("old-value"), "nemotron-super"), rebuilt)
        get.assert_called_once_with("nemotron-super")

    def test_a_current_or_keyless_provider_is_kept(self):
        current, keyless = KeyedProvider("same"), object()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "same"}), mock.patch("calls.get_provider") as get:
            self.assertIs(calls.current_provider(current, "nemotron-super"), current)
            self.assertIs(calls.current_provider(keyless, "nemotron-super"), keyless)
            self.assertIs(calls.current_provider(current, "fake-model"), current)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
