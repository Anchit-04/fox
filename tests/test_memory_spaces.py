import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loom"))

import memory
from orchestration import orchestrator as orch
from providers import ProviderResponse, ToolCall
from store import Store, project_space

WRITER = ROOT / "tests" / "memory_writer.py"


def started(workspace, space=None, parent_id=None) -> dict:
    return {"task": "t", "workspace": str(workspace), "mode": "agent", "permission_mode": "workspace-write",
            "model_key": None, "parent_id": parent_id, "memory_space": space}


class SpaceCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.workspace = self.folder / "shop"
        self.workspace.mkdir()
        self.db = self.folder / "fox.db"
        self.store = Store.open(self.db)
        self.addCleanup(self.store.close)
        self.space = project_space(self.workspace)

    def chat(self, session_id: str, shared: bool) -> memory.Memory:
        mem = memory.Memory.for_chat(self.store, session_id, self.workspace, shared)
        self.store.append_event(session_id, "session_started", started(self.workspace, mem.write_space))
        return mem


class Isolation(SpaceCase):
    def test_a_private_chat_and_a_shared_chat_cannot_read_each_other(self):
        private = self.chat("aaaa1111", shared=False)
        shared = self.chat("bbbb2222", shared=True)
        mine = private.add_note("a", "private detail")
        ours = shared.add_note("b", "project detail")
        self.assertEqual(mine, "note_1")
        self.assertEqual(ours, "bbbb.note_1")
        self.assertNotIn("private detail", shared.read_entry(mine))
        self.assertIn("No entry", shared.read_entry("note_2"))
        self.assertIn("No entry", private.read_entry(ours))
        self.assertNotIn("private detail", shared.read_index())
        self.assertNotIn("project detail", private.read_index())

    def test_shared_chats_in_one_project_read_each_others_entries(self):
        first = self.chat("aaaa1111", shared=True)
        second = self.chat("cccc3333", shared=True)
        first.append_entry("t1", "exec", "outcome: found it", kind="task_result", task_id="t1")
        second.append_entry("t1", "exec", "outcome: other", kind="task_result", task_id="t1")
        self.assertIn("found it", second.read_entry("aaaa.t1"))
        self.assertIn("found it", first.read_entry("t1"))
        self.assertIn("other", second.read_entry("t1"))
        row = self.store.memory_entry(self.space, "aaaa.t1")
        self.assertEqual((row["space_id"], row["session_id"]), (self.space, "aaaa1111"))

    def test_another_project_is_a_different_space(self):
        other = self.folder / "api"
        other.mkdir()
        here = self.chat("aaaa1111", shared=True)
        there = memory.Memory.for_chat(self.store, "dddd4444", other, shared=True)
        here.add_note("a", "shop only")
        self.assertNotEqual(here.write_space, there.write_space)
        self.assertEqual(there.read_index(), "(empty)")

    def test_chats_whose_ids_start_alike_get_longer_tags(self):
        self.chat("abcd1111", shared=True)
        late = memory.Memory.for_chat(self.store, "abcd2222", self.workspace, shared=True)
        self.assertEqual(late.prefix, "abcd2.")

    def test_memory_written_carries_the_space(self):
        shared = self.chat("aaaa1111", shared=True)
        shared.add_note("a", "x")
        [event] = self.store.events("aaaa1111", types=["memory_written"])
        self.assertEqual(event["payload"]["space_id"], self.space)

    def test_rebuilding_a_shared_chat_keeps_its_space(self):
        shared = self.chat("aaaa1111", shared=True)
        shared.add_note("a", "x")
        self.store.rebuild("aaaa1111")
        self.assertEqual(self.store.memory_entry(self.space, "aaaa.note_1")["content"], "x")
        self.assertEqual(self.store.session("aaaa1111")["memory_space"], self.space)


class TwoProcesses(SpaceCase):
    def test_two_writers_in_one_shared_space_never_mint_the_same_note_number(self):
        for session_id in ("aaaa1111", "bbbb2222"):
            self.store.append_event(session_id, "session_started", started(self.workspace, self.space))
        go = self.folder / "go"
        procs = [subprocess.Popen([sys.executable, str(WRITER), str(self.db), session_id, str(self.workspace),
                                   "40", str(go)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for session_id in ("aaaa1111", "bbbb2222")]
        go.touch()
        ids = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode(errors="replace"))
            ids += json.loads(out)
        numbers = sorted(int(entry_id.rsplit("_", 1)[1]) for entry_id in ids)
        self.assertEqual(numbers, list(range(1, 81)))
        self.assertEqual(len(self.store.memory_entries(self.space)), 80)


class LateJoiner(SpaceCase):
    def test_join_counts_the_backlog_and_lists_only_the_latest_headers(self):
        early = self.chat("aaaa1111", shared=True)
        for i in range(25):
            early.add_note("a", f"note {i}")
        early.append_entry("t1", "exec", "outcome", kind="task_result", task_id="t1")
        late = self.chat("bbbb2222", shared=True)
        seen, index = late.join()
        self.assertEqual(seen, 25)
        lines = index.splitlines()
        self.assertIn("26 entries", lines[0])
        self.assertIn("latest 20", lines[0])
        self.assertEqual(len(lines), 21)
        self.assertIn("id=aaaa.t1", lines[-1])
        self.assertNotIn("id=aaaa.note_1 ", index)
        self.assertIn("note 0", late.read_entry("aaaa.note_1"))

    def test_an_empty_space_gives_no_index(self):
        self.assertEqual(self.chat("aaaa1111", shared=True).join(), (0, ""))

    def test_the_orchestrator_starts_after_the_backlog_and_gets_only_new_notes(self):
        early = self.chat("aaaa1111", shared=True)
        for i in range(3):
            early.add_note("a", f"old {i}")
        late = self.chat("bbbb2222", shared=True)
        seen, _ = late.join()
        requests = []

        def generate(history, tools, system_prompt):
            requests.append("\n".join(turn.text or "" for turn in history))
            if len(requests) == 1:
                early.add_note("a", "fresh news")
                return ProviderResponse(text=None, tool_calls=[orch_wait()])
            return ProviderResponse(text="done", tool_calls=[])

        model = mock.Mock(generate=generate)
        with mock.patch.object(orch, "get_provider", lambda key: model):
            orch.run_orchestrator("go", model_key="claude-opus", mem=late, notes_seen=seen)
        self.assertNotIn("[memory]", requests[0])
        self.assertIn("id=aaaa.note_4", requests[1])
        self.assertNotIn("id=aaaa.note_1", requests[1])

    def test_entries_written_in_the_same_millisecond_keep_their_order(self):
        early = self.chat("aaaa1111", shared=True)
        with mock.patch("store._now", lambda: "2026-11-05T10:00:00.000Z"):
            ids = [early.add_note("a", f"n{i}") for i in range(12)]
        self.assertEqual([e["entry_id"] for e in early.entries()], ids)
        self.assertEqual(len(early.notes(skip=10)), 2)
        self.assertIn("id=aaaa.note_11", early.notes(skip=10)[0])


def orch_wait():
    return ToolCall("w", "wait_for_tasks", {})


if __name__ == "__main__":
    unittest.main()
