import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from orchestration.tasks import Lane, TaskStatus
from store import migrations
from store.migrations import MIGRATIONS, StoreError, migrate, version

TABLES = {"sessions", "events", "tasks", "memory_entries", "model_calls", "blobs", "workers"}


def fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    return db


def tables(db) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


class Migrate(unittest.TestCase):
    def test_fresh_database_gets_the_whole_schema(self):
        db = fresh()
        self.assertEqual(version(db), 0)
        self.assertEqual(migrate(db), len(MIGRATIONS))
        self.assertEqual(tables(db), TABLES)

    def test_migrating_twice_changes_nothing(self):
        db = fresh()
        migrate(db)
        self.assertEqual(migrate(db), len(MIGRATIONS))
        self.assertEqual(tables(db), TABLES)

    def test_a_version_1_database_is_upgraded_and_keeps_its_data(self):
        db = fresh()
        with mock.patch.object(migrations, "MIGRATIONS", MIGRATIONS[:1]):
            self.assertEqual(migrate(db), 1)
        db.execute("INSERT INTO sessions (id, workspace, task, mode, permission_mode, started_at) "
                   "VALUES ('old', 'w', 't', 'orchestrator', 'workspace-write', 'then')")
        self.assertNotIn("model_calls", tables(db))
        self.assertEqual(migrate(db), len(MIGRATIONS))
        self.assertIn("model_calls", tables(db))
        self.assertEqual(db.execute("SELECT task FROM sessions WHERE id = 'old'").fetchone(), ("t",))

    def test_existing_sessions_get_a_workspace_key_that_ignores_case_on_windows(self):
        db = fresh()
        with mock.patch.object(migrations, "MIGRATIONS", MIGRATIONS[:2]):
            migrate(db)
        for session_id, workspace in (("a", "C:/Users/me/Desktop/fixture"), ("b", "C:/Users/me/desktop/fixture/.")):
            db.execute("INSERT INTO sessions (id, workspace, task, mode, permission_mode, started_at) "
                       "VALUES (?, ?, 't', 'agent', 'workspace-write', 'then')", (session_id, workspace))
        migrate(db)
        keys = {row[0] for row in db.execute("SELECT workspace_key FROM sessions")}
        self.assertEqual(len(keys), 1 if sys.platform == "win32" else 2)

    def test_existing_tasks_start_at_run_one(self):
        db = fresh()
        with mock.patch.object(migrations, "MIGRATIONS", MIGRATIONS[:4]):
            migrate(db)
        db.execute("INSERT INTO sessions (id, workspace, workspace_key, task, mode, permission_mode, started_at, "
                   "memory_space) VALUES ('s', 'w', 'w', 't', 'agent', 'workspace-write', 'then', 's')")
        db.execute("INSERT INTO tasks (session_id, task_id, description, lane, scope, status, created_at) "
                   "VALUES ('s', 't1', 'd', 'read', '[]', 'done', 'then')")
        migrate(db)
        self.assertEqual(db.execute("SELECT task_id, run, status FROM tasks").fetchall(), [("t1", 1, "done")])

    def test_existing_memory_moves_into_each_sessions_own_space(self):
        db = fresh()
        with mock.patch.object(migrations, "MIGRATIONS", MIGRATIONS[:3]):
            migrate(db)
        for session_id, parent in (("root", None), ("side", "root"), ("other", None)):
            db.execute("INSERT INTO sessions (id, parent_id, workspace, workspace_key, task, mode, permission_mode, "
                       "started_at) VALUES (?, ?, 'w', 'w', 't', 'agent', 'workspace-write', 'then')",
                       (session_id, parent))
        for session_id, entry_id, created in (("root", "note_2", "b"), ("root", "note_1", "a"),
                                              ("other", "note_1", "a"), ("root", "t1", "c")):
            db.execute("INSERT INTO memory_entries (session_id, entry_id, agent, kind, content, created_at) "
                       "VALUES (?, ?, 'a', ?, 'x', ?)",
                       (session_id, entry_id, "note" if entry_id.startswith("note") else "task_result", created))
        migrate(db)
        self.assertEqual(dict(db.execute("SELECT id, memory_space FROM sessions")),
                         {"root": "root", "side": "root", "other": "other"})
        rows = db.execute("SELECT space_id, session_id, entry_id, position FROM memory_entries "
                          "ORDER BY space_id, position").fetchall()
        self.assertEqual(rows, [("other", "other", "note_1", 1), ("root", "root", "note_1", 1),
                                ("root", "root", "note_2", 2), ("root", "root", "t1", 3)])
        db.execute("INSERT INTO memory_entries (space_id, session_id, entry_id, agent, kind, content, created_at, "
                   "position) VALUES ('project:w', 'other', 'note_1', 'a', 'note', 'x', 'd', 1)")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO memory_entries (space_id, session_id, entry_id, agent, kind, content, "
                       "created_at, position) VALUES ('root', 'other', 'note_1', 'a', 'note', 'x', 'd', 9)")

    def test_a_newer_database_is_refused(self):
        db = fresh()
        db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
        with self.assertRaises(StoreError):
            migrate(db)
        self.assertEqual(tables(db), set())

    def test_a_failing_migration_leaves_no_trace(self):
        db = fresh()
        migrate(db)
        broken = MIGRATIONS + ["CREATE TABLE half_done (a TEXT);\nTHIS IS NOT SQL;"]
        with mock.patch.object(migrations, "MIGRATIONS", broken):
            with self.assertRaises(sqlite3.Error):
                migrate(db)
        self.assertEqual(version(db), len(MIGRATIONS))
        self.assertEqual(tables(db), TABLES)
        self.assertFalse(db.in_transaction)


class SchemaMatchesCode(unittest.TestCase):
    def setUp(self):
        self.db = fresh()
        migrate(self.db)
        self.db.execute("INSERT INTO sessions (id, workspace, task, mode, permission_mode, started_at) "
                        "VALUES ('s1', 'w', 't', 'orchestrator', 'workspace-write', 'now')")

    def insert_task(self, task_id, lane, status):
        self.db.execute("INSERT INTO tasks (session_id, task_id, description, lane, scope, status, created_at) "
                        "VALUES ('s1', ?, 'd', ?, '[]', ?, 'now')", (task_id, lane, status))

    def test_every_task_status_is_allowed(self):
        for i, status in enumerate(TaskStatus):
            with self.subTest(status=status):
                self.insert_task(f"t{i}", "read", status.value)

    def test_every_lane_is_allowed(self):
        for i, lane in enumerate(Lane):
            with self.subTest(lane=lane):
                self.insert_task(f"l{i}", lane.value, "queued")

    def insert_call(self, seq, cost, cost_source):
        self.db.execute("INSERT INTO model_calls (session_id, seq, model_key, duration_ms, input_tokens, "
                        "output_tokens, cache_read_tokens, cache_write_tokens, cost, cost_source, prefix, "
                        "ended_at) VALUES ('s1', ?, 'm', 10, 1, 1, 0, 0, ?, ?, '{}', 'now')",
                        (seq, cost, cost_source))

    def test_a_call_cost_always_says_where_it_came_from(self):
        self.insert_call(1, 0.01, "provider")
        self.insert_call(2, 0.01, "catalog")
        self.insert_call(3, None, None)
        for seq, cost, source in ((4, 0.01, None), (5, None, "catalog"), (6, 0.01, "guess")):
            with self.subTest(cost=cost, source=source), self.assertRaises(sqlite3.IntegrityError):
                self.insert_call(seq, cost, source)

    def test_values_the_code_does_not_define_are_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_task("x1", "read", "paused")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_task("x2", "coupled", "queued")


if __name__ == "__main__":
    unittest.main()
