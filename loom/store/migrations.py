import sqlite3
from pathlib import Path
from typing import Callable

from paths import workspace_key

HERE = Path(__file__).parent


def _add_workspace_key(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE sessions ADD COLUMN workspace_key TEXT")
    rows = db.execute("SELECT id, workspace FROM sessions").fetchall()
    db.executemany("UPDATE sessions SET workspace_key = ? WHERE id = ?",
                   [(workspace_key(workspace), session_id) for session_id, workspace in rows])
    db.execute("CREATE INDEX sessions_by_project ON sessions (workspace_key, started_at)")


def _add_memory_spaces(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE sessions ADD COLUMN memory_space TEXT")
    db.execute("UPDATE sessions SET memory_space = COALESCE(parent_id, id)")
    db.execute(
        "CREATE TABLE memory_entries_spaced ("
        "space_id TEXT NOT NULL, "
        "session_id TEXT NOT NULL REFERENCES sessions(id), "
        "entry_id TEXT NOT NULL, "
        "task_id TEXT, "
        "agent TEXT NOT NULL, "
        "kind TEXT NOT NULL CHECK (kind IN ('note', 'task_result')), "
        "scope TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(scope)), "
        "content TEXT NOT NULL, "
        "created_at TEXT NOT NULL, "
        "position INTEGER NOT NULL, "
        "PRIMARY KEY (space_id, entry_id)) WITHOUT ROWID")
    db.execute("INSERT INTO memory_entries_spaced (space_id, session_id, entry_id, task_id, agent, kind, scope, "
               "content, created_at, position) SELECT session_id, session_id, entry_id, task_id, agent, kind, scope, "
               "content, created_at, ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY created_at, entry_id) "
               "FROM memory_entries")
    db.execute("DROP TABLE memory_entries")
    db.execute("ALTER TABLE memory_entries_spaced RENAME TO memory_entries")
    db.execute("CREATE INDEX memory_by_session ON memory_entries (session_id)")
    db.execute("CREATE UNIQUE INDEX memory_by_position ON memory_entries (space_id, position)")
    db.execute("CREATE INDEX sessions_by_memory_space ON sessions (memory_space)")


MIGRATIONS: list[str | Callable[[sqlite3.Connection], None]] = [
    (HERE / "schema.sql").read_text(encoding="utf-8"),
    (HERE / "model_calls.sql").read_text(encoding="utf-8"),
    _add_workspace_key,
    _add_memory_spaces,
]


class StoreError(Exception):
    pass


def version(db: sqlite3.Connection) -> int:
    return db.execute("PRAGMA user_version").fetchone()[0]


def _run(db: sqlite3.Connection, number: int, step) -> None:
    if callable(step):
        if db.in_transaction:
            db.commit()
        db.execute("BEGIN")
        step(db)
        db.execute(f"PRAGMA user_version = {number}")
        db.execute("COMMIT")
    else:
        db.executescript(f"BEGIN;\n{step}\nPRAGMA user_version = {number};\nCOMMIT;")


def migrate(db: sqlite3.Connection) -> int:
    current = version(db)
    if current > len(MIGRATIONS):
        raise StoreError(f"fox.db is at schema version {current}, but this Fox only understands up to "
                         f"{len(MIGRATIONS)}. Update Fox before opening it.")
    for number in range(current + 1, len(MIGRATIONS) + 1):
        try:
            _run(db, number, MIGRATIONS[number - 1])
        except sqlite3.Error:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return version(db)
