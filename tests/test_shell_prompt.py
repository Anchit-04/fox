import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
from orchestration import orchestrator
from sandbox import sandbox_client


class ShellIsStatedExactly(unittest.TestCase):
    def test_the_agent_prompt_carries_the_shell_note(self):
        self.assertIn(sandbox_client.SHELL_NOTE, agent.build_system_prompt("some-model"))

    def test_the_orchestrator_prompt_carries_the_shell_note(self):
        self.assertIn(sandbox_client.SHELL_NOTE, orchestrator.SYSTEM_PROMPT)

    def test_the_vague_windows_line_is_gone(self):
        self.assertNotIn("cmd/PowerShell", agent.build_system_prompt("some-model"))

    def test_the_bash_tool_says_it_runs_cmd(self):
        self.assertIn("cmd.exe", agent.BASH_TOOL["description"])
        self.assertNotIn("bash command", agent.BASH_TOOL["description"])
        self.assertIn("cmd.exe", agent.BASH_TOOL["parameters"]["properties"]["command"]["description"])

    def test_the_note_states_the_real_limits(self):
        self.assertIn(f"{sandbox_client.COMMAND_TIMEOUT_SECONDS} seconds", sandbox_client.SHELL_NOTE)
        self.assertIn(f"{sandbox_client.MAX_OUTPUT_CHARS} characters", sandbox_client.SHELL_NOTE)

    def test_the_client_waits_longer_than_sandboxd_does(self):
        self.assertGreater(sandbox_client.CLIENT_READ_TIMEOUT, sandbox_client.COMMAND_TIMEOUT_SECONDS)

    def test_sandboxd_still_uses_the_same_timeout(self):
        main_go = Path(sandbox_client.__file__).with_name("sandboxd") / "main.go"
        self.assertIn(f"DefaultTimeout             = {sandbox_client.COMMAND_TIMEOUT_SECONDS} * time.Second",
                      main_go.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
