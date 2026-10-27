import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from store import Store, StoreError
from store.migrations import MIGRATIONS

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def started(**extra) -> dict:
    return {"task": "rename get_user", "workspace": "C:/fixture", "mode": "orchestrator",
            "permission_mode": "workspace-write", "model_key": "nemotron-super", "parent_id": None, **extra}


class StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "fox.db"
        self.store = self.open()

    def open(self, readonly=False) -> Store:
        store = Store.open(self.path, readonly=readonly)
        self.addCleanup(store.close)
        return store

    def start(self, session_id="s1", **extra) -> None:
        self.store.append_event(session_id, "session_started", started(**extra))

    def row(self, sql, *params):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, params).fetchone()


class Opening(StoreCase):
    def test_creates_and_migrates_the_file(self):
        self.assertTrue(self.path.exists())
        self.assertEqual(self.row("PRAGMA user_version")[0], len(MIGRATIONS))

    def test_uses_wal(self):
        self.assertEqual(self.row("PRAGMA journal_mode")[0], "wal")

    def test_reopening_keeps_the_data(self):
        self.start()
        self.store.close()
        again = self.open()
        self.assertEqual([e["type"] for e in again.events("s1")], ["session_started"])


class Appending(StoreCase):
    def test_seq_counts_per_session(self):
        self.start("s1")
        self.start("s2")
        seqs = [self.store.append_event("s1", "tool_call", {"task_id": None, "name": "read_file", "args": {}})
                for _ in range(2)]
        self.assertEqual(seqs, [2, 3])
        self.assertEqual([e["seq"] for e in self.store.events("s2")], [1])

    def test_timestamps_are_utc_milliseconds_and_never_go_back(self):
        self.start()
        for _ in range(20):
            self.store.append_event("s1", "error", {"task_id": None, "message": "x"})
        stamps = [e["ts"] for e in self.store.events("s1")]
        self.assertTrue(all(TS.match(ts) for ts in stamps), stamps[0])
        self.assertEqual(stamps, sorted(stamps))

    def test_payload_round_trips_and_task_id_gets_its_column(self):
        self.start()
        payload = {"task_id": "t3", "name": "edit_file", "args": {"path": "café.py", "n": [1, 2]}}
        self.store.append_event("s1", "tool_call", payload)
        (event,) = self.store.events("s1", types=["tool_call"])
        self.assertEqual((event["task_id"], event["payload"]), ("t3", payload))

    def test_values_json_cannot_hold_are_stored_as_text(self):
        self.start()
        self.store.append_event("s1", "tool_result", {"task_id": None, "name": "x", "content": Path("a/b")})
        (event,) = self.store.events("s1", types=["tool_result"])
        self.assertEqual(event["payload"]["content"], str(Path("a/b")))

    def test_filtering_by_type(self):
        self.start()
        self.store.append_event("s1", "error", {"task_id": None, "message": "x"})
        self.store.append_event("s1", "agent_turn", {"task_id": None, "model_key": "m", "text": "hi"})
        self.assertEqual([e["type"] for e in self.store.events("s1", types=["error", "agent_turn"])],
                         ["error", "agent_turn"])
        self.assertEqual(self.store.events("s1", types=["tool_call"]), [])

    def test_threads_never_skip_or_repeat_a_seq(self):
        self.start()
        errors = []

        def write(n):
            try:
                for i in range(50):
                    self.store.append_event("s1", "tool_call", {"task_id": f"t{n}", "name": "x", "args": {"i": i}})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(32)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual([e["seq"] for e in self.store.events("s1")], list(range(1, 32 * 50 + 2)))

    def test_a_failed_append_leaves_nothing_and_the_next_one_works(self):
        self.start()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_event("ghost", "error", {"task_id": None, "message": "no such session"})
        self.assertEqual(self.store.events("ghost"), [])
        self.assertEqual(self.store.append_event("s1", "error", {"task_id": None, "message": "fine"}), 2)

    def test_a_bad_session_started_saves_neither_event_nor_session(self):
        with self.assertRaises(KeyError):
            self.store.append_event("s1", "session_started", {"task": "no workspace"})
        self.assertEqual(self.store.events("s1"), [])
        self.assertIsNone(self.row("SELECT id FROM sessions WHERE id = 's1'"))


class SessionProjection(StoreCase):
    def test_session_started_creates_the_row(self):
        self.start(model_key="gemini-lite")
        row = self.row("SELECT workspace, task, mode, permission_mode, model_key, status, started_at, ended_at "
                       "FROM sessions WHERE id = 's1'")
        ts = self.store.events("s1")[0]["ts"]
        self.assertEqual(row, ("C:/fixture", "rename get_user", "orchestrator", "workspace-write",
                               "gemini-lite", "running", ts, None))

    def test_side_conversation_points_at_its_parent(self):
        self.start("parent")
        self.start("side", parent_id="parent")
        self.assertEqual(self.row("SELECT parent_id FROM sessions WHERE id = 'side'")[0], "parent")

    def test_session_ended_sets_status_text_and_end_time(self):
        self.start()
        self.store.append_event("s1", "session_ended", {"status": "done", "final_text": "Renamed it."})
        end_ts = self.store.events("s1", types=["session_ended"])[0]["ts"]
        self.assertEqual(self.row("SELECT status, final_text, ended_at FROM sessions WHERE id = 's1'"),
                         ("done", "Renamed it.", end_ts))


class Projections(StoreCase):
    def setUp(self):
        super().setUp()
        self.start()

    def emit(self, event_type, **payload):
        return self.store.append_event("s1", event_type, payload)

    def task(self, task_id="t1"):
        return self.row("SELECT lane, scope, status, model_key, report, error, created_at, started_at, ended_at "
                        "FROM tasks WHERE session_id = 's1' AND task_id = ?", task_id)

    def ts(self, seq):
        return next(e["ts"] for e in self.store.events("s1") if e["seq"] == seq)

    def test_a_task_from_created_to_done(self):
        created = self.emit("task_created", task_id="t1", description="find callers", lane="write", scope=["a.py"])
        running = self.emit("task_status_changed", task_id="t1", status="running", error=None, report=None,
                            model_key="gemini-lite")
        report = {"outcome": "2 callers", "changes": ["a.py"], "validation": "ran tests", "blockers": []}
        done = self.emit("task_status_changed", task_id="t1", status="done", error=None, report=report,
                         model_key=None)
        self.assertEqual(self.task(), ("write", '["a.py"]', "done", "gemini-lite", json.dumps(report),
                                       None, self.ts(created), self.ts(running), self.ts(done)))

    def test_a_failed_task_keeps_its_error(self):
        self.emit("task_created", task_id="t1", description="d", lane="write", scope=["a.py"])
        failed = self.emit("task_status_changed", task_id="t1", status="failed",
                           error="files already claimed by a running task: a.py", report=None, model_key=None)
        lane, scope, status, model_key, report, error, created_at, started_at, ended_at = self.task()
        self.assertEqual((status, error, started_at, ended_at),
                         ("failed", "files already claimed by a running task: a.py", None, self.ts(failed)))

    def test_running_twice_keeps_the_first_start(self):
        clock = iter(f"2026-10-26T10:00:0{i}.000Z" for i in range(10))
        with mock.patch("store._now", lambda: next(clock)):
            self.emit("task_created", task_id="t1", description="d", lane="read", scope=[])
            first = self.emit("task_status_changed", task_id="t1", status="running", error=None, report=None,
                              model_key="m")
            self.emit("task_status_changed", task_id="t1", status="needs-approval", error=None, report=None,
                      model_key=None)
            self.emit("task_status_changed", task_id="t1", status="running", error=None, report=None,
                      model_key=None)
        stamps = [e["ts"] for e in self.store.events("s1")]
        self.assertEqual(len(set(stamps[1:])), len(stamps) - 1)
        self.assertEqual(self.task()[7], self.ts(first))

    def test_a_status_for_an_unknown_task_is_logged_and_changes_nothing(self):
        self.emit("task_status_changed", task_id="t9", status="done", error=None, report=None, model_key=None)
        self.assertEqual(self.store.events("s1", types=["task_status_changed"])[0]["task_id"], "t9")
        self.assertIsNone(self.task("t9"))

    def test_only_the_top_agent_sets_the_session_model_and_todos(self):
        self.emit("agent_turn", task_id=None, model_key="claude-opus", text="planning")
        self.emit("todo_updated", task_id=None, checklist="- [ ] rename")
        self.emit("agent_turn", task_id="t1", model_key="gemini-lite", text="reading")
        self.emit("todo_updated", task_id="t1", checklist="- [ ] read a.py")
        self.assertEqual(self.row("SELECT model_key, todos FROM sessions WHERE id = 's1'"),
                         ("claude-opus", "- [ ] rename"))

    def test_memory_notes_and_task_results(self):
        note = self.emit("memory_written", task_id="t1", entry_id="note_1", agent="gemini-lite", kind="note",
                         scope=[], content="get_user is called in api.py")
        self.emit("memory_written", task_id="t1", entry_id="t1", agent="gemini-lite", kind="task_result",
                  scope=["a.py"], content="outcome: 2 callers")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT entry_id, task_id, agent, kind, scope, content, created_at "
                              "FROM memory_entries ORDER BY created_at, entry_id").fetchall()
        self.assertEqual(rows[0], ("note_1", "t1", "gemini-lite", "note", "[]",
                                   "get_user is called in api.py", self.ts(note)))
        self.assertEqual(rows[1][:5], ("t1", "t1", "gemini-lite", "task_result", '["a.py"]'))

    def test_a_duplicate_memory_id_is_refused_and_not_logged(self):
        self.emit("memory_written", task_id=None, entry_id="note_1", agent="a", kind="note", scope=[], content="x")
        with self.assertRaises(sqlite3.IntegrityError):
            self.emit("memory_written", task_id=None, entry_id="note_1", agent="a", kind="note", scope=[],
                      content="y")
        self.assertEqual(len(self.store.events("s1", types=["memory_written"])), 1)


class Rebuild(StoreCase):
    def play(self):
        self.start("s1")
        self.start("side", parent_id="s1")
        steps = [
            ("agent_turn", {"task_id": None, "model_key": "nemotron-super", "text": "planning"}),
            ("todo_updated", {"task_id": None, "checklist": "- [ ] rename"}),
            ("task_created", {"task_id": "t1", "description": "find callers", "lane": "read", "scope": []}),
            ("task_status_changed", {"task_id": "t1", "status": "running", "error": None, "report": None,
                                     "model_key": "gemini-lite"}),
            ("memory_written", {"task_id": "t1", "entry_id": "note_1", "agent": "gemini-lite", "kind": "note",
                                "scope": [], "content": "two callers"}),
            ("tool_call", {"task_id": "t1", "name": "read_file", "args": {"path": "a.py"}}),
            ("task_status_changed", {"task_id": "t1", "status": "done", "error": None, "model_key": None,
                                     "report": {"outcome": "2 callers", "changes": [], "validation": "read",
                                                "blockers": []}}),
            ("task_created", {"task_id": "t2", "description": "rename", "lane": "write", "scope": ["a.py"]}),
            ("task_status_changed", {"task_id": "t2", "status": "failed", "error": "crashed", "report": None,
                                     "model_key": None}),
            ("memory_written", {"task_id": "t1", "entry_id": "t1", "agent": "gemini-lite", "kind": "task_result",
                                "scope": [], "content": "outcome: 2 callers"}),
            ("session_ended", {"status": "done", "final_text": "Renamed."}),
        ]
        for event_type, payload in steps:
            self.store.append_event("s1", event_type, payload)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {
                "sessions": db.execute("SELECT * FROM sessions WHERE id = 's1'").fetchall(),
                "tasks": db.execute("SELECT * FROM tasks WHERE session_id = 's1' ORDER BY task_id").fetchall(),
                "memory": db.execute("SELECT * FROM memory_entries WHERE session_id = 's1' "
                                     "ORDER BY entry_id").fetchall(),
            }

    def test_rebuilding_gives_identical_tables(self):
        self.play()
        before = self.snapshot()
        self.assertEqual((len(before["tasks"]), len(before["memory"])), (2, 2))
        self.store.rebuild("s1")
        self.assertEqual(self.snapshot(), before)

    def test_rebuilding_repairs_a_tampered_row(self):
        self.play()
        before = self.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE tasks SET status = 'cancelled', error = 'tampered' WHERE task_id = 't1'")
            db.execute("DELETE FROM memory_entries WHERE entry_id = 'note_1'")
            db.commit()
        self.assertNotEqual(self.snapshot(), before)
        self.store.rebuild("s1")
        self.assertEqual(self.snapshot(), before)

    def test_a_parent_with_a_side_conversation_can_be_rebuilt(self):
        self.play()
        self.store.rebuild("s1")
        self.assertEqual(self.row("SELECT parent_id FROM sessions WHERE id = 'side'")[0], "s1")

    def test_a_read_only_store_cannot_rebuild(self):
        self.play()
        with self.assertRaises(StoreError):
            self.open(readonly=True).rebuild("s1")


class ReadOnly(StoreCase):
    def test_sees_what_the_writer_committed(self):
        self.start()
        reader = self.open(readonly=True)
        self.store.append_event("s1", "error", {"task_id": None, "message": "later"})
        self.assertEqual([e["seq"] for e in reader.events("s1")], [1, 2])

    def test_cannot_write(self):
        self.start()
        reader = self.open(readonly=True)
        with self.assertRaises(StoreError):
            reader.append_event("s1", "error", {"task_id": None, "message": "x"})

    def test_the_connection_itself_is_read_only(self):
        self.start()
        reader = self.open(readonly=True)
        with self.assertRaises(sqlite3.OperationalError):
            reader._db.execute("DELETE FROM events")
        self.assertEqual(len(self.store.events("s1")), 1)

    def test_refuses_a_missing_file(self):
        with self.assertRaises(StoreError):
            Store.open(self.path.with_name("missing.db"), readonly=True)

    def test_refuses_a_file_that_was_never_migrated(self):
        empty = self.path.with_name("empty.db")
        sqlite3.connect(empty).close()
        with self.assertRaises(StoreError):
            Store.open(empty, readonly=True)


if __name__ == "__main__":
    unittest.main()
