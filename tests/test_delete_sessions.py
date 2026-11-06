import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from providers import Image, ToolResult, Turn
from store import Store, project_space
from store.history_log import HistoryRecorder

SHOT = Image("image/jpeg", base64.b64encode(b"jpeg bytes").decode())
OTHER = Image("image/jpeg", base64.b64encode(b"other bytes").decode())


class DeletingChats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.store = Store.open(self.workspace / "fox.db")
        self.addCleanup(self.store.close)
        self.space = project_space(self.workspace)

    def chat(self, session_id, parent=None, shared=False):
        self.store.append_event(session_id, "session_started", {
            "task": f"task {session_id}", "workspace": str(self.workspace), "mode": "orchestrator",
            "permission_mode": "workspace-write", "model_key": "claude-haiku", "parent_id": parent,
            "memory_space": self.space if shared else (parent or session_id)})
        self.store.append_event(session_id, "task_created", {"task_id": "t1", "description": "d", "lane": "read",
                                                             "scope": []})
        self.store.append_event(session_id, "model_called", {
            "task_id": None, "model_key": "claude-haiku", "duration_ms": 1, "input_tokens": 1, "output_tokens": 1,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "cost": None, "cost_source": None, "prefix": {}})

    def count(self, table, session_id):
        column = "root_id" if table == "workers" else ("id" if table == "sessions" else "session_id")
        return self.store._query(f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (session_id,))[0]["n"]

    def test_a_chat_and_its_side_chats_leave_no_rows_behind(self):
        self.chat("root")
        self.chat("side", parent="root")
        self.chat("keep")
        self.store.register_worker("root", 1, 2, "tok", str(self.workspace))
        removed = self.store.delete_session("root")
        self.assertEqual(sorted(removed), ["root", "side"])
        for table in ("sessions", "events", "tasks", "model_calls", "workers"):
            for gone in ("root", "side"):
                self.assertEqual(self.count(table, gone), 0, (table, gone))
        self.assertEqual(self.count("sessions", "keep"), 1)
        self.assertEqual(self.store._query("PRAGMA foreign_key_check"), [])

    def test_private_notes_go_and_shared_notes_stay(self):
        self.chat("private")
        self.chat("shared", shared=True)
        self.store.append_note("private", "private", "", {"task_id": None, "agent": "a", "scope": [], "content": "mine"})
        self.store.append_note("shared", self.space, "", {"task_id": None, "agent": "a", "scope": [],
                                                          "content": "for the project"})
        self.store.delete_session("private")
        self.store.delete_session("shared")
        notes = self.store.memory_entries([self.space, "private"])
        self.assertEqual([(n["content"], n["session_id"]) for n in notes], [("for the project", None)])

    def test_only_unused_screenshots_are_dropped(self):
        self.chat("a")
        self.chat("b")
        HistoryRecorder(self.store, "a", None).result(ToolResult("c", "browser_screenshot", "x", images=[SHOT]))
        HistoryRecorder(self.store, "b", None).result(ToolResult("c", "browser_screenshot", "x", images=[SHOT, OTHER]))
        self.store.delete_session("b")
        self.assertEqual(len(self.store._query("SELECT id FROM blobs")), 1)
        self.store.delete_session("a")
        self.assertEqual(self.store._query("SELECT id FROM blobs"), [])

    def test_archived_chats_drop_out_of_listings_until_asked_for(self):
        self.chat("old")
        self.chat("new")
        self.store.append_event("old", "session_archived", {"task_id": None, "archived": True})
        self.assertEqual([r["session_id"] for r in self.store.sessions()], ["new"])
        listed = {r["session_id"]: r["archived"] for r in self.store.sessions(include_archived=True)}
        self.assertEqual(listed, {"old": True, "new": False})
        self.store.append_event("old", "session_archived", {"task_id": None, "archived": False})
        self.assertEqual(len(self.store.sessions()), 2)

    def test_deleting_an_unknown_chat_does_nothing(self):
        self.assertEqual(self.store.delete_session("ghost"), [])


if __name__ == "__main__":
    unittest.main()
