import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

from providers import Image, ToolCall, ToolResult, Turn
from providers.anthropic import _to_anthropic_messages
from providers.gemini import _to_gemini_contents
from providers.openai_compatible import _to_openai_messages
from store import Store
from store.history_log import (INTERRUPTED_RESULT, HistoryRecorder, blob_id, decode, deserialize_turn, encode,
                               rebuild_history, serialize_turn)

SHOT = Image("image/jpeg", base64.b64encode(b"\xff\xd8 a fake jpeg").decode())


class Blobs:
    def __init__(self):
        self.saved: dict[str, Image] = {}

    def put(self, image: Image) -> str:
        key = blob_id(image)
        self.saved[key] = image
        return key

    def get(self, key: str) -> Image:
        return self.saved[key]


def round_trip(turns: list[Turn], blobs: Blobs | None = None) -> list[Turn]:
    blobs = blobs or Blobs()
    stored = json.loads(json.dumps([serialize_turn(turn, blobs.put) for turn in turns]))
    return [deserialize_turn(data, blobs.get) for data in stored]


def conversation(raw=None, call_raw=None) -> list[Turn]:
    return [
        Turn(role="user", text="fix the cart total"),
        Turn(role="assistant", text="Reading it first.", raw=raw,
             tool_calls=[ToolCall(id="call_1", name="read_file", args={"path": "shop/cart.py", "lines": [1, 40]},
                                  raw=call_raw)]),
        Turn(role="user", tool_results=[ToolResult(call_id="call_1", name="read_file", content="def total(): ...",
                                                   images=[SHOT])]),
        Turn(role="user", text="[Message from human]: also check tax", images=[SHOT]),
    ]


class Encoding(unittest.TestCase):
    def test_bytes_survive_json(self):
        value = {"thought_signature": b"\x00\x01\xfe", "nested": [b"ab", {"k": 1}]}
        self.assertEqual(decode(json.loads(json.dumps(encode(value)))), value)

    def test_sdk_objects_become_plain_dicts(self):
        block = ToolUseBlock(id="tu_1", name="read_file", input={"path": "a.py"}, type="tool_use")
        self.assertEqual(encode([block]), [{"id": "tu_1", "input": {"path": "a.py"}, "name": "read_file",
                                            "type": "tool_use"}])

    def test_identical_images_share_one_blob(self):
        blobs = Blobs()
        round_trip(conversation(), blobs)
        self.assertEqual(len(blobs.saved), 1)

    def test_a_plain_turn_keeps_its_shape(self):
        turn = Turn(role="assistant", text="(no reply)")
        self.assertEqual(serialize_turn(turn, Blobs().put), {"role": "assistant", "text": "(no reply)"})
        self.assertEqual(round_trip([turn]), [turn])


class EachProviderSendsTheSameRequest(unittest.TestCase):
    def test_anthropic_with_thinking_and_tool_blocks(self):
        raw = [ThinkingBlock(thinking="the bug is in total()", signature="sig-abc", type="thinking"),
               TextBlock(text="Reading it first.", type="text"),
               ToolUseBlock(id="call_1", name="read_file", input={"path": "shop/cart.py", "lines": [1, 40]},
                            type="tool_use")]
        original = conversation(raw=raw)
        self.assertEqual(encode(_to_anthropic_messages(round_trip(original))),
                         encode(_to_anthropic_messages(original)))

    def test_gemini_keeps_the_thought_signature_bytes(self):
        original = conversation(call_raw={"thought_signature": b"\x9a\x00signed-by-gemini"})
        rebuilt = round_trip(original)
        self.assertEqual(rebuilt[1].tool_calls[0].raw["thought_signature"], b"\x9a\x00signed-by-gemini")
        self.assertEqual([c.model_dump() for c in _to_gemini_contents(rebuilt)],
                         [c.model_dump() for c in _to_gemini_contents(original)])

    def test_openai_compatible(self):
        original = conversation()
        self.assertEqual(_to_openai_messages(round_trip(original)), _to_openai_messages(original))

    def test_the_rebuilt_turns_equal_the_originals(self):
        original = conversation(call_raw={"thought_signature": b"sig"})
        self.assertEqual(round_trip(original), original)


class RecordAndRebuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store.open(Path(self.tmp.name) / "fox.db")
        self.addCleanup(self.store.close)
        self.store.append_event("s1", "session_started", {
            "task": "fix the cart total", "workspace": self.tmp.name, "mode": "agent",
            "permission_mode": "workspace-write", "model_key": "claude-haiku", "parent_id": None})

    def record(self, turns, task_id=None):
        recorder = HistoryRecorder(self.store, "s1", task_id)
        for turn in turns:
            if turn.tool_results and turn.role == "user" and turn.text is None:
                for result in turn.tool_results:
                    recorder.result(result)
            else:
                recorder.turn(turn)
        return recorder

    def test_a_recorded_conversation_rebuilds_exactly(self):
        original = conversation(call_raw={"thought_signature": b"sig"})
        self.record(original)
        self.assertEqual(rebuild_history(self.store, "s1"), original)

    def test_each_agent_has_its_own_history(self):
        self.record(conversation())
        executor = [Turn(role="user", text="t1: add tests"), Turn(role="assistant", text="done")]
        self.record(executor, task_id="t1")
        self.assertEqual(rebuild_history(self.store, "s1", "t1"), executor)
        self.assertEqual(len(rebuild_history(self.store, "s1")), 4)
        self.assertEqual(self.store.history_agents("s1"), [None, "t1"])

    def test_compaction_starts_the_history_again(self):
        recorder = self.record(conversation())
        recorder.compacted("Original task: fix the cart total. Plan: 1 of 3 done.")
        recorder.turn(Turn(role="assistant", text="Carrying on."))
        self.assertEqual(rebuild_history(self.store, "s1"), [
            Turn(role="user", text="Original task: fix the cart total. Plan: 1 of 3 done."),
            Turn(role="assistant", text="Carrying on."),
        ])

    def test_results_written_one_by_one_fold_into_one_turn(self):
        recorder = HistoryRecorder(self.store, "s1")
        recorder.turn(Turn(role="assistant", tool_calls=[ToolCall(id="a", name="read_file", args={}),
                                                         ToolCall(id="b", name="list_directory", args={})]))
        recorder.result(ToolResult(call_id="a", name="read_file", content="one"))
        recorder.result(ToolResult(call_id="b", name="list_directory", content="two"))
        rebuilt = rebuild_history(self.store, "s1")
        self.assertEqual(len(rebuilt), 2)
        self.assertEqual([r.content for r in rebuilt[1].tool_results], ["one", "two"])

    def test_a_crash_between_tools_answers_the_rest_as_interrupted(self):
        recorder = HistoryRecorder(self.store, "s1")
        recorder.turn(Turn(role="assistant", tool_calls=[ToolCall(id="a", name="write_file", args={}),
                                                         ToolCall(id="b", name="run_bash_command", args={})]))
        recorder.result(ToolResult(call_id="a", name="write_file", content="wrote cart.py"))
        results = rebuild_history(self.store, "s1")[-1].tool_results
        self.assertEqual([(r.call_id, r.content) for r in results],
                         [("a", "wrote cart.py"), ("b", INTERRUPTED_RESULT)])

    def test_a_crash_before_any_result_answers_every_call(self):
        HistoryRecorder(self.store, "s1").turn(
            Turn(role="assistant", tool_calls=[ToolCall(id="a", name="browser_click", args={"element": 3})]))
        rebuilt = rebuild_history(self.store, "s1")
        self.assertEqual(rebuilt[-1], Turn(role="user", tool_results=[
            ToolResult(call_id="a", name="browser_click", content=INTERRUPTED_RESULT)]))

    def test_events_chain_through_parent_and_a_new_recorder_continues_it(self):
        first = self.record(conversation()[:2])
        second = HistoryRecorder(self.store, "s1")
        self.assertEqual(second.parent, first.parent)
        seq = second.turn(Turn(role="user", text="[Message from human]: go on"))
        events = self.store.history_events("s1")
        self.assertEqual([e["payload"]["parent"] for e in events], [None] + [e["seq"] for e in events[:-1]])
        self.assertEqual(events[-1]["seq"], seq)

    def test_images_go_to_blobs_once_and_not_into_events(self):
        self.record(conversation())
        rows = self.store._query("SELECT id, size FROM blobs")
        self.assertEqual(len(rows), 1)
        payloads = json.dumps([e["payload"] for e in self.store.history_events("s1")])
        self.assertNotIn(SHOT.data, payloads)
        self.assertEqual(rebuild_history(self.store, "s1")[2].tool_results[0].images, [SHOT])

    def test_history_events_do_not_touch_the_projections(self):
        before = self.store.session("s1")
        self.record(conversation())
        self.assertEqual(self.store.session("s1"), before)


if __name__ == "__main__":
    unittest.main()
