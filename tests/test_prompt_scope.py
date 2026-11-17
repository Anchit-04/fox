import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
from config import roster as rosters
from orchestration import orchestrator
from sandbox import sandbox_client
from tools import file_tools, process_tools


def orchestrator_prompt() -> str:
    run = orchestrator._Run(model_key="some-model", mode="workspace-write", mem=mock.Mock(), registry=mock.Mock(),
                            todo=mock.Mock())
    with mock.patch.object(orchestrator.roster, "load", return_value=rosters.Roster()), \
            mock.patch.object(orchestrator.roster, "workers", return_value=[]):
        return orchestrator._system_prompt(run)


class NotOnlyACodingAgent(unittest.TestCase):
    def test_the_identity_names_work_beyond_code(self):
        self.assertNotIn("a coding agent", agent.IDENTITY)
        for word in ("installing tools", "media", "running"):
            self.assertIn(word, agent.IDENTITY)

    def test_both_prompts_carry_the_approach(self):
        self.assertIn(agent.APPROACH, agent.build_system_prompt("some-model"))
        self.assertIn(agent.APPROACH, orchestrator_prompt())

    def test_the_approach_says_to_check_tools_and_install(self):
        self.assertIn("Before saying you can't do something", agent.APPROACH)
        self.assertIn("where <name>", agent.APPROACH)
        self.assertIn("winget install", agent.APPROACH)

    def test_code_rules_sit_under_their_own_heading(self):
        prompt = agent.build_system_prompt("some-model")
        self.assertLess(prompt.index("When working on code:"), prompt.index("Always read a file before editing"))


class LongJobs(unittest.TestCase):
    def test_the_shell_note_routes_long_jobs_to_start_process(self):
        note = sandbox_client.SHELL_NOTE
        self.assertIn("a download, an install, an ffmpeg encode", note)
        self.assertIn("goes through start_process", note)
        self.assertIn("can't open or control desktop apps", note)

    def test_the_limit_is_two_minutes_everywhere(self):
        self.assertEqual(sandbox_client.COMMAND_TIMEOUT_SECONDS, 120)
        self.assertIn("120 seconds", process_tools.START_PROCESS_TOOL["description"])
        self.assertIn("120 seconds", agent.build_system_prompt("some-model"))


class DelegationRule(unittest.TestCase):
    def test_the_rule_is_countable(self):
        prompt = orchestrator.SYSTEM_PROMPT
        self.assertIn("Delegate a part only if all three hold", prompt)
        self.assertIn("at least 3 tool calls, or reading more than about 300 lines", prompt)
        self.assertNotIn("Make connected edits yourself", prompt)

    def test_delegated_parts_are_not_investigated_first(self):
        self.assertIn("your first calls are list_directory", orchestrator.SYSTEM_PROMPT)
        self.assertIn("Don't read files, search or run commands for those parts first", orchestrator.SYSTEM_PROMPT)

    def test_the_users_notes_beat_the_size_check(self):
        self.assertIn("goes to that worker even when it is small", orchestrator.SYSTEM_PROMPT)
        roster = rosters.Roster(notes={"codex": "every data job"})
        with mock.patch.object(rosters, "workers", return_value=["codex"]):
            note = rosters.render_for_prompt(roster, "codex")
        self.assertIn("however small it looks", note)
        self.assertIn("the size check does not", note)


class ListDirectory(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / ".gitignore").write_text("x\n", encoding="utf-8")
        (self.root / "app.py").write_text("a\nb\nc", encoding="utf-8")
        (self.root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
        (self.root / "src").mkdir()
        (self.root / "src" / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
        patcher = mock.patch.object(file_tools, "_resolve", side_effect=lambda p: self.root / p)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dotfiles_keep_their_dot(self):
        entries = json.loads(file_tools.list_directory("."))["entries"]
        self.assertIn(".gitignore", entries)
        self.assertNotIn("gitignore", entries)

    def test_text_files_get_line_counts_and_binaries_do_not(self):
        lines = json.loads(file_tools.list_directory("."))["lines"]
        self.assertEqual(lines["app.py"], 3)
        self.assertEqual(lines[str(Path("src") / "main.go")], 3)
        self.assertNotIn("logo.png", lines)


if __name__ == "__main__":
    unittest.main()
