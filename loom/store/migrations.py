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


MIGRATIONS: list[str | Callable[[sqlite3.Connection], None]] = [
    (HERE / "schema.sql").read_text(encoding="utf-8"),
    (HERE / "model_calls.sql").read_text(encoding="utf-8"),
    _add_workspace_key,
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
