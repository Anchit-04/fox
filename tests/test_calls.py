import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from calls import prefix_digest, timed_generate
from providers import ProviderResponse, ToolCall, ToolResult, Turn, Usage

TOOLS = [{"type": "function", "name": "read_file", "description": "Read a file.",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]


class FakeProvider:
    def __init__(self, usage=Usage(), error=None):
        self.usage = usage
        self.error = error

    def generate(self, history, tools, system_prompt):
        if self.error:
            raise self.error
        return ProviderResponse(text="ok", tool_calls=[], usage=self.usage)


def conversation():
    return [
        Turn(role="user", text="rename get_user"),
        Turn(role="assistant", text="reading", tool_calls=[ToolCall("c1", "read_file", {"path": "a.py"})]),
        Turn(role="user", tool_results=[ToolResult("c1", "read_file", "def get_user(): ...")]),
    ]


class TimedGenerate(unittest.TestCase):
    def call(self, provider, model_key="claude-opus", task_id="t1"):
        events = []
        response = timed_generate(provider, conversation(), TOOLS, "You are Fox.", model_key=model_key,
                                  task_id=task_id, event_sink=lambda e, p: events.append((e, p)))
        return response, events

    def test_one_call_is_one_event_with_its_usage_and_cost(self):
        usage = Usage(input_tokens=1_000, output_tokens=200, cache_read_tokens=10_000, cache_write_tokens=500)
        response, [(event, payload)] = self.call(FakeProvider(usage))
        self.assertEqual(response.text, "ok")
        self.assertEqual(event, "model_called")
        self.assertEqual({k: payload[k] for k in ("task_id", "model_key", "input_tokens", "output_tokens",
                                                   "cache_read_tokens", "cache_write_tokens", "cost_source")},
                         {"task_id": "t1", "model_key": "claude-opus", "input_tokens": 1_000, "output_tokens": 200,
                          "cache_read_tokens": 10_000, "cache_write_tokens": 500, "cost_source": "catalog"})
        self.assertAlmostEqual(payload["cost"], (1_000 * 4 + 200 * 20 + 10_000 * 0.2 + 500 * 5) / 1_000_000)
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertGreaterEqual(payload["duration_ms"], 0)

    def test_the_orchestrator_logs_without_a_task(self):
        _, [(_, payload)] = self.call(FakeProvider(), task_id=None)
        self.assertIsNone(payload["task_id"])

    def test_a_bill_from_the_provider_is_used(self):
        _, [(_, payload)] = self.call(FakeProvider(Usage(input_tokens=10, reported_cost=0.0)), model_key="nemotron-super")
        self.assertEqual((payload["cost"], payload["cost_source"]), (0.0, "provider"))

    def test_a_failed_call_logs_nothing_and_raises(self):
        with self.assertRaises(TimeoutError):
            self.call(FakeProvider(error=TimeoutError("slow")))

    def test_works_without_an_event_sink(self):
        response = timed_generate(FakeProvider(), conversation(), TOOLS, "s", model_key="m", task_id=None,
                                  event_sink=None)
        self.assertEqual(response.text, "ok")


class PrefixDigest(unittest.TestCase):
    def test_the_same_request_gives_the_same_digest(self):
        self.assertEqual(prefix_digest(conversation(), TOOLS, "s"), prefix_digest(conversation(), TOOLS, "s"))

    def test_one_part_per_tools_system_and_message(self):
        digest = prefix_digest(conversation(), TOOLS, "s")
        self.assertEqual(len(digest["messages"]), 3)
        self.assertEqual(len(digest["tools"]), 12)

    def test_a_changed_system_prompt_changes_only_the_system_part(self):
        before, after = prefix_digest(conversation(), TOOLS, "s"), prefix_digest(conversation(), TOOLS, "s + index")
        self.assertNotEqual(before["system"], after["system"])
        self.assertEqual((before["tools"], before["messages"]), (after["tools"], after["messages"]))

    def test_a_new_turn_leaves_the_earlier_messages_alone(self):
        before = prefix_digest(conversation(), TOOLS, "s")
        after = prefix_digest(conversation() + [Turn(role="assistant", text="done")], TOOLS, "s")
        self.assertEqual(after["messages"][:3], before["messages"])
        self.assertEqual(len(after["messages"]), 4)

    def test_an_edited_old_message_is_pinpointed(self):
        edited = conversation()
        edited[2] = Turn(role="user", tool_results=[ToolResult("c1", "read_file", "def fetch_user(): ...")])
        before, after = prefix_digest(conversation(), TOOLS, "s"), prefix_digest(edited, TOOLS, "s")
        changed = [i for i, (a, b) in enumerate(zip(before["messages"], after["messages"])) if a != b]
        self.assertEqual(changed, [2])

    def test_tool_key_order_does_not_matter(self):
        reordered = [{"parameters": TOOLS[0]["parameters"], "description": "Read a file.", "name": "read_file",
                      "type": "function"}]
        self.assertEqual(prefix_digest([], TOOLS, "s")["tools"], prefix_digest([], reordered, "s")["tools"])


if __name__ == "__main__":
    unittest.main()
