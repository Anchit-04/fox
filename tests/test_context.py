import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
import context
import memory
from providers import ProviderResponse, ToolCall, ToolResult, Turn


class HistorySize(unittest.TestCase):
    def test_counts_text_tool_args_and_results(self):
        history = [
            Turn(role="user", text="abcd"),
            Turn(role="assistant", tool_calls=[ToolCall(id="c1", name="read_file", args={"path": "a"})]),
            Turn(role="user", tool_results=[ToolResult(call_id="c1", name="read_file", content="x" * 10)]),
        ]
        self.assertEqual(context.history_chars(history), 4 + len('{"path": "a"}') + 10)

    def test_compacts_only_at_the_threshold(self):
        with mock.patch.object(context, "COMPACT_AT_CHARS", 10):
            self.assertFalse(context.should_compact([Turn(role="user", text="x" * 9)]))
            self.assertTrue(context.should_compact([Turn(role="user", text="x" * 10)]))


class Recorder:
    def __init__(self, script):
        self.script = list(script)
        self.histories = []

    def generate(self, history, tools, system_prompt):
        self.histories.append(list(history))
        return self.script.pop(0) if self.script else ProviderResponse(text="done", tool_calls=[])


def read_step(i):
    return ProviderResponse(text=None, tool_calls=[ToolCall(id=f"c{i}", name="memory_read", args={"entry_id": "x"})])


class AgentCompaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = memory.Memory.scratch()

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, model):
        with mock.patch.object(agent, "get_provider", return_value=model):
            agent.run_agent("task", verbose=False, interactive=False, mem=self.mem)

    def test_one_read_per_turn_keeps_everything_it_read(self):
        model = Recorder([read_step(i) for i in range(15)])
        self.run_with(model)
        self.assertEqual(model.histories[-1][0].text, "task")
        self.assertEqual(len(model.histories[-1]), 31)

    def test_a_history_over_the_threshold_is_replaced_by_a_summary(self):
        model = Recorder([read_step(0)])
        with mock.patch.object(context, "COMPACT_AT_CHARS", 1):
            self.run_with(model)
        [only] = model.histories[1]
        self.assertIn("Original task: task", only.text)
        self.assertIn("memory_read", only.text)


if __name__ == "__main__":
    unittest.main()
