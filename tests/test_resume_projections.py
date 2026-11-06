import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from store import Store, migrations
from store.migrations import MIGRATIONS, migrate


def started(store, session_id="s1", parent=None):
    store.append_event(session_id, "session_started", {
        "task": "fix it", "workspace": "/w/shop", "mode": "orchestrator", "permission_mode": "workspace-write",
        "model_key": "claude-haiku", "parent_id": parent})


class StatusMigration(unittest.TestCase):
    def test_existing_chats_keep_every_row_and_link(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        db.execute("PRAGMA foreign_keys = ON")
        with mock.patch.object(migrations, "MIGRATIONS", MIGRATIONS[:5]):
            migrate(db)
        db.execute("INSERT INTO sessions (id, workspace, workspace_key, task, mode, permission_mode, started_at, "
                   "memory_space, status, final_text) VALUES ('root', 'w', 'w', 't', 'agent', 'workspace-write', "
                   "'then', 'root', 'failed', 'interrupted: server restarted')")
        db.execute("INSERT INTO sessions (id, parent_id, workspace, workspace_key, task, mode, permission_mode, "
                   "started_at, memory_space) VALUES ('side', 'root', 'w', 'w', 't', 'agent', 'workspace-write', "
                   "'then', 'root')")
        db.execute("INSERT INTO events (session_id, seq, ts, type, payload) VALUES ('root', 1, 'then', 'x', '{}')")
        migrate(db)
        self.assertEqual(db.execute("SELECT id, parent_id, status FROM sessions ORDER BY id").fetchall(),
                         [("root", None, "failed"), ("side", "root", "running")])
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        db.execute("UPDATE sessions SET status = 'interrupted' WHERE id = 'root'")
        db.execute("UPDATE sessions SET status = 'paused' WHERE id = 'side'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE sessions SET status = 'exploded' WHERE id = 'root'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO events (session_id, seq, ts, type, payload) VALUES ('ghost', 1, 't', 'x', '{}')")
        indexes = {row[1] for row in db.execute("PRAGMA index_list(sessions)")}
        self.assertTrue({"sessions_by_project", "sessions_by_memory_space"} <= indexes)


class ResumeProjections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store.open(Path(self.tmp.name) / "fox.db")
        self.addCleanup(self.store.close)
        started(self.store)

    def test_an_interrupted_chat_comes_back_as_running(self):
        self.store.append_event("s1", "session_ended", {"task_id": None, "status": "interrupted",
                                                        "final_text": "interrupted: server restarted"})
        self.assertEqual(self.store.session("s1")["status"], "interrupted")
        self.store.append_event("s1", "session_resumed", {"task_id": None, "reason": "worker gone",
                                                          "model_key": "gemini-flash"})
        row = self.store.session("s1")
        self.assertEqual((row["status"], row["final_text"], row["ended_at"], row["model_key"]),
                         ("running", None, None, "gemini-flash"))

    def test_a_reopened_task_runs_again_with_its_run_counted(self):
        self.store.append_event("s1", "task_created", {"task_id": "t1", "description": "d", "lane": "read",
                                                       "scope": []})
        self.store.append_event("s1", "task_status_changed", {"task_id": "t1", "status": "running"})
        self.store.append_event("s1", "task_status_changed", {"task_id": "t1", "status": "done",
                                                              "report": {"outcome": "ok"}})
        self.store.append_event("s1", "task_reopened", {"task_id": "t1", "run": 2})
        task = self.store._query("SELECT status, run, report, ended_at FROM tasks")[0]
        self.assertEqual(task, {"status": "running", "run": 2, "report": None, "ended_at": None})

    def test_rebuilding_from_events_gives_the_same_rows(self):
        self.store.append_event("s1", "session_ended", {"task_id": None, "status": "paused", "final_text": "stopped"})
        self.store.append_event("s1", "session_resumed", {"task_id": None, "reason": "your message"})
        before = self.store.session("s1")
        self.store.rebuild("s1")
        self.assertEqual(self.store.session("s1"), before)


if __name__ == "__main__":
    unittest.main()
