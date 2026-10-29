import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import projections
from .migrations import MIGRATIONS, StoreError, migrate, version


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    def __init__(self, db: sqlite3.Connection, readonly: bool):
        self._db = db
        self._readonly = readonly
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: str | Path, readonly: bool = False) -> "Store":
        path = Path(path)
        if readonly:
            if not path.exists():
                raise StoreError(f"{path} does not exist yet. Start the server once to create it.")
            db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                                 check_same_thread=False, isolation_level=None)
            found = version(db)
            if found != len(MIGRATIONS):
                db.close()
                raise StoreError(f"{path} is at schema version {found}, but this Fox expects {len(MIGRATIONS)}. "
                                 "Start the server once to upgrade it.")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        if not readonly:
            migrate(db)
        return cls(db, readonly)

    @contextmanager
    def _write(self):
        if self._readonly:
            raise StoreError("this store was opened read-only")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def append_event(self, session_id: str, event_type: str, payload: dict) -> int:
        with self._write() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE session_id = ?",
                             (session_id,)).fetchone()[0]
            ts = _now()
            db.execute(
                "INSERT INTO events (session_id, seq, ts, task_id, type, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, seq, ts, payload.get("task_id"), event_type, json.dumps(payload, default=str)),
            )
            projections.apply(db, session_id, seq, ts, event_type, payload)
        return seq

    def rebuild(self, session_id: str) -> None:
        with self._write() as db:
            db.execute("PRAGMA defer_foreign_keys = ON")
            db.execute("DELETE FROM memory_entries WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            rows = db.execute("SELECT seq, ts, type, payload FROM events WHERE session_id = ? ORDER BY seq",
                              (session_id,)).fetchall()
            for seq, ts, event_type, payload in rows:
                projections.apply(db, session_id, seq, ts, event_type, json.loads(payload))

    def events(self, session_id: str, types: list[str] | None = None) -> list[dict]:
        sql = "SELECT seq, ts, task_id, type, payload FROM events WHERE session_id = ?"
        params: list = [session_id]
        if types:
            sql += f" AND type IN ({', '.join('?' * len(types))})"
            params += list(types)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY seq", params).fetchall()
        return [{"seq": seq, "ts": ts, "task_id": task_id, "type": event_type, "payload": json.loads(payload)}
                for seq, ts, task_id, event_type, payload in rows]

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cursor = self._db.execute(sql, params)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def session(self, session_id: str) -> dict | None:
        rows = self._query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return rows[0] if rows else None

    def session_view(self, session_id: str) -> dict | None:
        session = self.session(session_id)
        if session is None:
            return None
        return {**session, "session_id": session_id, "turns": self.events(session_id)}

    def memory_entries(self, session_id: str) -> list[dict]:
        return self._query("SELECT * FROM memory_entries WHERE session_id = ? ORDER BY created_at, entry_id",
                           (session_id,))

    def memory_entry(self, session_id: str, entry_id: str) -> dict | None:
        rows = self._query("SELECT * FROM memory_entries WHERE session_id = ? AND entry_id = ?",
                           (session_id, entry_id))
        return rows[0] if rows else None

    def next_note_id(self, session_id: str) -> str:
        count = self._query("SELECT COUNT(*) AS n FROM memory_entries WHERE session_id = ? AND kind = 'note'",
                            (session_id,))[0]["n"]
        return f"note_{count + 1}"

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
