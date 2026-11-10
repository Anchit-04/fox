import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import external
from config import roster as rosters
from config import routing, vault
from orchestration import dispatch
from orchestration.claims import FileClaims
from orchestration.tasks import Lane, Task
from providers.config import MODEL_REGISTRY, price_text

KEYS = {"gemini-lite", "nemotron-super", "kimi-k2"}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "roster.json"
        self.patches = [
            mock.patch.object(vault, "is_present", side_effect=lambda key: key in KEYS),
            mock.patch.object(vault, "validate_key", side_effect=lambda key: key in KEYS),
            mock.patch.object(external, "installed", return_value=["claude-code"]),
            mock.patch.object(external, "available", side_effect=lambda name: name == "claude-code"),
            mock.patch.object(rosters, "ROSTER_FILE", self.path),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def write(self, data):
        self.path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


class Loading(Fixture):
    def test_no_file_means_no_preferences(self):
        self.assertEqual(rosters.load(), rosters.Roster())

    def test_a_full_file(self):
        self.write({"default_worker": "kimi-k2", "default_chat": "nemotron-super",
                    "notes": {"kimi-k2": "good at CSS"}, "off": ["gemini-lite"]})
        r = rosters.load()
        self.assertEqual((r.default_worker, r.default_chat, r.notes, r.off, r.problems),
                         ("kimi-k2", "nemotron-super", {"kimi-k2": "good at CSS"}, frozenset({"gemini-lite"}), ()))

    def test_broken_json_is_ignored_with_a_reason(self):
        self.write("{not json")
        r = rosters.load()
        self.assertIsNone(r.default_worker)
        self.assertIn("was ignored", r.problems[0])

    def test_wrong_shapes_and_unknown_names_are_reported(self):
        self.write({"default_worker": 3, "notes": ["x"], "off": ["kimi"]})
        problems = " ".join(rosters.load().problems)
        self.assertIn("default_worker must be a model name", problems)
        self.assertIn("notes must map", problems)
        self.assertIn("unknown names: kimi", problems)


class Workers(Fixture):
    def test_every_model_with_a_key_and_every_installed_cli(self):
        expected = [k for k in MODEL_REGISTRY if k in KEYS] + ["claude-code"]
        self.assertEqual(rosters.workers(rosters.Roster()), expected)

    def test_off_removes_a_worker(self):
        self.assertNotIn("kimi-k2", rosters.workers(rosters.Roster(off=frozenset({"kimi-k2"}))))


class Prompt(Fixture):
    def render(self, roster=None, default="gemini-lite"):
        return rosters.render_for_prompt(roster or rosters.Roster(), default)

    def test_lists_names_prices_and_the_default(self):
        text = self.render()
        lines = {line.split(":")[0][2:]: line for line in text.splitlines() if line.startswith("- ")}
        self.assertIn("## Workers you can delegate to", text)
        for name in KEYS:
            self.assertIn(price_text(name), lines[name])
            self.assertEqual("sees images" in lines[name], bool(MODEL_REGISTRY[name].get("vision")))
        self.assertTrue(lines["gemini-lite"].endswith("· default"))
        self.assertIn("Leave model_key out to use the default worker, gemini-lite.", text)
        self.assertNotIn("claude-opus", text)

    def test_external_clis_are_described_once(self):
        text = self.render()
        self.assertIn("- claude-code: external CLI", text)
        self.assertEqual(text.count("can't call ask_orchestrator"), 1)

    def test_the_users_notes_ride_along(self):
        text = self.render(rosters.Roster(notes={"kimi-k2": "good at CSS"}))
        kimi = [line for line in text.splitlines() if line.startswith("- kimi-k2:")][0]
        self.assertTrue(kimi.endswith("· the user's note: good at CSS"))

    def test_problems_are_shown(self):
        self.assertIn("roster.json: unknown names: kimi", self.render(rosters.Roster(problems=("unknown names: kimi",))))

    def test_nothing_available(self):
        with mock.patch.object(rosters, "workers", return_value=[]):
            self.assertIn("do the work yourself", self.render(default=None))

    def test_no_word_tier_anywhere(self):
        self.assertNotIn("tier", self.render().lower())
        self.assertTrue(all("tier" not in entry for entry in MODEL_REGISTRY.values()))


class PickExecutor(Fixture):
    def test_a_named_worker_wins(self):
        self.write({"default_worker": "gemini-lite"})
        self.assertEqual(routing.pick_executor("kimi-k2", fallback="nemotron-super"), "kimi-k2")

    def test_a_wrong_name_lists_the_real_ones(self):
        with self.assertRaises(routing.RoutingError) as caught:
            routing.pick_executor("kimi")
        self.assertIn("isn't a known model", str(caught.exception))
        self.assertIn("kimi-k2", str(caught.exception))

    def test_a_model_without_a_key(self):
        with self.assertRaisesRegex(routing.RoutingError, "has no API key"):
            routing.pick_executor("deepseek-chat")

    def test_a_model_turned_off(self):
        self.write({"off": ["kimi-k2"]})
        with self.assertRaisesRegex(routing.RoutingError, "turned off"):
            routing.pick_executor("kimi-k2")

    def test_the_default_worker_comes_next(self):
        self.write({"default_worker": "kimi-k2"})
        self.assertEqual(routing.pick_executor(fallback="nemotron-super"), "kimi-k2")

    def test_an_unusable_default_worker_is_an_error_not_a_silent_swap(self):
        self.write({"default_worker": "deepseek-chat"})
        with self.assertRaisesRegex(routing.RoutingError, "default_worker in roster.json"):
            routing.pick_executor(fallback="nemotron-super")

    def test_with_no_default_the_orchestrators_own_model(self):
        self.assertEqual(routing.pick_executor(fallback="nemotron-super"), "nemotron-super")

    def test_no_usable_default_is_an_error_never_a_guess(self):
        self.write({"off": ["nemotron-super"]})
        with self.assertRaises(routing.RoutingError) as caught:
            routing.pick_executor(fallback="nemotron-super")
        self.assertIn("No default worker", str(caught.exception))
        self.assertIn("default_worker in roster.json", str(caught.exception))

    def test_nothing_available(self):
        with mock.patch.object(rosters, "workers", return_value=[]):
            with self.assertRaisesRegex(routing.RoutingError, "No worker is available"):
                routing.pick_executor()

    def test_default_worker_helper_never_raises(self):
        self.write({"default_worker": "deepseek-chat"})
        self.assertIsNone(routing.default_worker("nemotron-super"))


class PickChat(Fixture):
    def test_default_chat(self):
        self.write({"default_chat": "kimi-k2"})
        self.assertEqual(routing.pick_chat(), "kimi-k2")

    def test_an_external_cli_cannot_host_a_chat_yet(self):
        self.write({"default_chat": "claude-code"})
        with self.assertRaisesRegex(routing.RoutingError, "can't run on one yet"):
            routing.pick_chat()

    def test_with_no_choice_it_asks_instead_of_guessing(self):
        with self.assertRaisesRegex(routing.RoutingError, "No model chosen"):
            routing.pick_chat()


class DispatchPassesTheOrchestratorsModel(Fixture):
    def test_fallback_reaches_the_picker(self):
        seen = []

        def pick(preferred=None, fallback=None):
            seen.append((preferred, fallback))
            raise routing.RoutingError("stop here")

        task = Task(id="t1", description="x", lane=Lane.READ, scope=[])
        inbox = dispatch.TaskInbox()
        with mock.patch.object(dispatch.routing, "pick_executor", side_effect=pick):
            dispatch.start_task(task, "workspace-write", claims=FileClaims(), inbox=inbox, mem=mock.Mock(),
                                default_model="nemotron-super")
            inbox.wait(10)
        self.assertEqual(seen, [(None, "nemotron-super")])


if __name__ == "__main__":
    unittest.main()
