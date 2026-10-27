import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from orchestration.tasks import Lane, TaskStatus
from store import migrations
from store.migrations import MIGRATIONS, StoreError, migrate, version

TABLES = {"sessions", "events", "tasks", "memory_entries"}


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

    def test_values_the_code_does_not_define_are_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_task("x1", "read", "paused")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_task("x2", "coupled", "queued")


if __name__ == "__main__":
    unittest.main()
