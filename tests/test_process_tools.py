import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
import approval
import permissions
from orchestration.policy import READ_LANE_TOOLS
from sandbox import sandbox_client
from tools import process_tools

WW = permissions.WORKSPACE_WRITE
RO = permissions.READ_ONLY


class Requests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        patcher = mock.patch.object(sandbox_client, "_ask", side_effect=self.answer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reply = {"exit_code": 0, "output": "", "process": "p1", "status": "running"}

    def answer(self, request):
        self.sent.append(request)
        return dict(self.reply)

    def test_each_tool_sends_its_op(self):
        process_tools.PROCESS_TOOL_HANDLERS["start_process"]({"command": "npm run dev"})
        process_tools.PROCESS_TOOL_HANDLERS["read_process"]({"process": "p1"})
        process_tools.PROCESS_TOOL_HANDLERS["stop_process"]({"process": "p1"})

        self.assertEqual(self.sent, [{"op": "start", "command": "npm run dev"},
                                     {"op": "read", "process": "p1"},
                                     {"op": "stop", "process": "p1"}])

    def test_read_without_a_process_lists_them(self):
        process_tools.PROCESS_TOOL_HANDLERS["read_process"]({})
        self.assertEqual(self.sent, [{"op": "read", "process": ""}])

    def test_long_output_keeps_the_newest_part(self):
        self.reply["output"] = "old" * 15000 + "newest line"

        result = json.loads(sandbox_client.read_process("p1"))

        self.assertTrue(result["output"].endswith("newest line"))
        self.assertTrue(result["output"].startswith("[... "))
        self.assertLessEqual(len(result["output"]), sandbox_client.MAX_OUTPUT_CHARS + 50)
        self.assertEqual(result["status"], "running")


class Wiring(unittest.TestCase):
    def test_agent_offers_and_runs_the_three_tools(self):
        names = {t["name"] for t in agent.ALL_TOOLS}
        for name in ("start_process", "read_process", "stop_process"):
            self.assertIn(name, names)
            self.assertTrue(agent._is_known_tool(name, None))

    def test_prompt_points_long_running_work_at_start_process(self):
        self.assertIn("start_process", agent.build_system_prompt("some-model"))
        self.assertIn(f"at most {sandbox_client.MAX_BACKGROUND_PROCESSES} run at once", sandbox_client.SHELL_NOTE)

    def test_the_cap_matches_sandboxd(self):
        process_go = Path(sandbox_client.__file__).with_name("sandboxd") / "process.go"
        self.assertIn(f"MaxBackgroundProcesses        = {sandbox_client.MAX_BACKGROUND_PROCESSES}",
                      process_go.read_text(encoding="utf-8"))

    def test_read_lane_gets_the_process_tools(self):
        for name in ("start_process", "read_process", "stop_process"):
            self.assertIn(name, READ_LANE_TOOLS)

    def test_approval_names_a_background_start(self):
        self.assertEqual(approval.describe("start_process", {"command": "npm run dev"}),
                         "start in the background: npm run dev")


class Permissions(unittest.TestCase):
    def classify(self, name, args, mode=WW):
        return permissions.classify(name, args, mode).action

    def test_starting_is_judged_like_a_bash_command(self):
        for command in ("npm run dev", "dir", "git push --force"):
            for mode in (RO, WW, permissions.FULL_ACCESS):
                self.assertEqual(self.classify("start_process", {"command": command}, mode),
                                 self.classify("run_bash_command", {"command": command}, mode),
                                 f"{command!r} in {mode}")

    def test_a_server_needs_approval_and_is_refused_read_only(self):
        self.assertEqual(self.classify("start_process", {"command": "npm run dev"}), permissions.ASK)
        self.assertEqual(self.classify("start_process", {"command": "npm run dev"}, RO), permissions.DENY)

    def test_reading_and_stopping_need_no_approval(self):
        for name in ("read_process", "stop_process"):
            self.assertEqual(self.classify(name, {"process": "p1"}, RO), permissions.ALLOW)


@unittest.skipUnless(sandbox_client._SANDBOXD_PATH.exists(), "sandboxd.exe is not built")
class RealSandboxd(unittest.TestCase):
    def test_start_read_stop(self):
        started = json.loads(sandbox_client.start_process(
            "for /L %i in (1,1,30) do @(echo tick %i & ping -n 2 127.0.0.1 >nul)"))
        self.assertEqual(started["status"], "running")
        self.assertIn("tick 1", started["output"])

        time.sleep(2)
        listed = json.loads(sandbox_client.read_process())
        self.assertIn(f"{started['process']}  running", listed["output"])

        stopped = json.loads(sandbox_client.stop_process(started["process"]))
        self.assertEqual(stopped["status"], "stopped")
        self.assertIn("tick", stopped["output"])

    def test_a_bash_command_still_works_alongside(self):
        self.assertEqual(json.loads(sandbox_client.run_bash_command("echo hi")),
                         {"exit_code": 0, "output": "hi\r\n"})


if __name__ == "__main__":
    unittest.main()
