import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import permissions
from orchestration.policy import (
    READ_LANE_TOOLS, PolicyRefusal, admit, lane_from_proposal,
)
from orchestration.tasks import Lane, Task

RO, WW, FA = permissions.READ_ONLY, permissions.WORKSPACE_WRITE, permissions.FULL_ACCESS


def task(lane: Lane) -> Task:
    return Task(id="t1", description="d", lane=lane, scope=[])


def names(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools}


def real_tools() -> list[dict]:
    import agent
    return agent.ALL_TOOLS


class ReadLane(unittest.TestCase):
    def test_reader_is_read_only_in_every_session_mode(self):
        for mode in permissions.MODES:
            with self.subTest(mode=mode):
                a = admit(task(Lane.READ), mode)
                self.assertEqual(a.mode, RO)

    def test_reader_gets_no_write_tools(self):
        got = names(admit(task(Lane.READ), WW).tools(real_tools()))
        self.assertNotIn("write_file", got)
        self.assertNotIn("edit_file", got)

    def test_reader_keeps_what_it_needs_to_investigate(self):
        got = names(admit(task(Lane.READ), WW).tools(real_tools()))
        for tool in ("read_file", "list_directory", "search_files", "run_bash_command",
                     "todo_write", "memory_read", "memory_write"):
            self.assertIn(tool, got)

    def test_unknown_tool_is_withheld_from_readers(self):
        tools = [{"name": "read_file"}, {"name": "move_file"}, {"name": "some_mcp_tool"}]
        self.assertEqual(names(admit(task(Lane.READ), WW).tools(tools)), {"read_file"})

    def test_allowlist_contains_no_modifying_path_tool(self):
        modifying = {n for n, (_, m) in permissions.PATH_TOOLS.items() if m}
        self.assertFalse(READ_LANE_TOOLS & modifying)


class WriteLane(unittest.TestCase):
    def test_writer_takes_the_session_mode(self):
        for mode in (WW, FA):
            with self.subTest(mode=mode):
                self.assertEqual(admit(task(Lane.WRITE), mode).mode, mode)

    def test_writer_gets_every_tool(self):
        tools = real_tools()
        self.assertEqual(admit(task(Lane.WRITE), WW).tools(tools), tools)

    def test_tools_returns_a_copy(self):
        tools = [{"name": "write_file"}]
        out = admit(task(Lane.WRITE), WW).tools(tools)
        out.append({"name": "extra"})
        self.assertEqual(len(tools), 1)

    def test_writer_in_read_only_session_is_refused_not_downgraded(self):
        with self.assertRaises(PolicyRefusal) as ctx:
            admit(task(Lane.WRITE), RO)
        self.assertIn("t1", str(ctx.exception))


class Admission(unittest.TestCase):
    def test_unknown_session_mode_is_a_bug(self):
        for lane in Lane:
            with self.subTest(lane=lane), self.assertRaises(ValueError):
                admit(task(lane), "yolo")

    def test_admission_is_frozen(self):
        a = admit(task(Lane.WRITE), WW)
        with self.assertRaises(AttributeError):
            a.mode = FA


class LaneFromProposal(unittest.TestCase):
    def test_only_exact_read_is_a_read(self):
        self.assertIs(lane_from_proposal("read"), Lane.READ)

    def test_unsure_means_write(self):
        for value in ("write", None, "", "READ", "Read", " read", "reader", "readonly"):
            with self.subTest(value=value):
                self.assertIs(lane_from_proposal(value), Lane.WRITE)


if __name__ == "__main__":
    unittest.main()
