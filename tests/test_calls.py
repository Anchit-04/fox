import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from calls import generate_or_pause, model_error_kind, prefix_digest, timed_generate
from history import History
from injection import InjectionQueue
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


class StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code


class FailsOnce:
    def __init__(self, error):
        self.error = error

    def generate(self, history, tools, system_prompt):
        raise self.error


class ModelErrorKind(unittest.TestCase):
    def test_fixable_statuses_have_a_kind(self):
        kinds = {code: model_error_kind(StatusError(code)) for code in (401, 403, 402, 404, 429, 503)}
        self.assertEqual(kinds, {401: "key", 403: "key", 402: "credits", 404: "model", 429: "rate_limit",
                                 503: "provider"})

    def test_a_gemini_style_code_counts(self):
        error = Exception("expired")
        error.code = 401
        self.assertEqual(model_error_kind(error), "key")

    def test_other_errors_have_no_kind(self):
        self.assertIsNone(model_error_kind(StatusError(400)))
        self.assertIsNone(model_error_kind(TimeoutError("slow")))


class GenerateOrPause(unittest.TestCase):
    def call(self, provider, queue):
        events = []
        history = History("rename get_user")
        with mock.patch("calls.get_provider", return_value=FakeProvider()) as rebuilt:
            result = generate_or_pause(provider, history, TOOLS, "s", model_key="nemotron-super", task_id=None,
                                       event_sink=lambda e, p: events.append((e, p)), injection_queue=queue)
        return result, history, events, rebuilt

    def test_a_dead_key_pauses_until_a_message_then_retries_with_a_fresh_provider(self):
        queue = InjectionQueue()
        queue.push(None, "retry")
        (provider, response), history, events, rebuilt = self.call(FailsOnce(StatusError(401)), queue)
        self.assertEqual(response.text, "ok")
        self.assertIs(provider, rebuilt.return_value)
        rebuilt.assert_called_once_with("nemotron-super")
        self.assertEqual([e for e, _ in events], ["model_error", "human_message_injected", "model_called"])
        self.assertEqual(events[0][1]["kind"], "key")
        self.assertEqual(history.turns[-1].text, "[Message from human]: retry")

    def test_without_a_queue_the_error_still_ends_the_run(self):
        with self.assertRaises(StatusError):
            self.call(FailsOnce(StatusError(401)), None)

    def test_an_unfixable_error_is_not_paused_on(self):
        queue = InjectionQueue()
        queue.push(None, "retry")
        with self.assertRaises(StatusError):
            self.call(FailsOnce(StatusError(400)), queue)

    def test_closing_the_session_while_paused_raises_the_original_error(self):
        queue = InjectionQueue()
        queue.close(None)
        with self.assertRaises(StatusError):
            self.call(FailsOnce(StatusError(402)), queue)


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
