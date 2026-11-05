import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from paths import workspace_key

from . import projections
from .migrations import MIGRATIONS, StoreError, migrate as run_migrations, version


PROJECT_SPACE = "project:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def project_space(workspace: str | Path) -> str:
    return PROJECT_SPACE + workspace_key(workspace)


def is_project_space(space_id: str | None) -> bool:
    return bool(space_id) and space_id.startswith(PROJECT_SPACE)


def _append(db: sqlite3.Connection, session_id: str, event_type: str, payload: dict) -> int:
    seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE session_id = ?",
                     (session_id,)).fetchone()[0]
    ts = _now()
    db.execute(
        "INSERT INTO events (session_id, seq, ts, task_id, type, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, seq, ts, payload.get("task_id"), event_type, json.dumps(payload, default=str)),
    )
    projections.apply(db, session_id, seq, ts, event_type, payload)
    return seq


class Store:
    def __init__(self, db: sqlite3.Connection, readonly: bool):
        self._db = db
        self._readonly = readonly
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: str | Path, readonly: bool = False, migrate: bool = True) -> "Store":
        path = Path(path)
        if readonly:
            if not path.exists():
                raise StoreError(f"{path} does not exist yet. Start the server once to create it.")
            db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                                 check_same_thread=False, isolation_level=None)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        if readonly or not migrate:
            found = version(db)
            if found != len(MIGRATIONS):
                db.close()
                raise StoreError(f"{path} is at schema version {found}, but this Fox expects {len(MIGRATIONS)}. "
                                 "Start the server once to upgrade it.")
        else:
            run_migrations(db)
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
            return _append(db, session_id, event_type, payload)

    def append_note(self, session_id: str, space_id: str, prefix: str, payload: dict) -> tuple[int, str]:
        with self._write() as db:
            count = db.execute("SELECT COUNT(*) FROM memory_entries WHERE space_id = ? AND kind = 'note'",
                               (space_id,)).fetchone()[0]
            entry_id = f"{prefix}note_{count + 1}"
            payload = {**payload, "entry_id": entry_id, "space_id": space_id, "kind": "note"}
            return _append(db, session_id, "memory_written", payload), entry_id

    def rebuild(self, session_id: str) -> None:
        with self._write() as db:
            db.execute("PRAGMA defer_foreign_keys = ON")
            db.execute("DELETE FROM model_calls WHERE session_id = ?", (session_id,))
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

    def memory_entries(self, spaces: str | list[str]) -> list[dict]:
        spaces = [spaces] if isinstance(spaces, str) else list(spaces)
        return self._query(f"SELECT * FROM memory_entries WHERE space_id IN ({', '.join('?' * len(spaces))}) "
                           "ORDER BY position, space_id", tuple(spaces))

    def memory_entry(self, spaces: str | list[str], entry_id: str) -> dict | None:
        spaces = [spaces] if isinstance(spaces, str) else list(spaces)
        rows = self._query(f"SELECT * FROM memory_entries WHERE space_id IN ({', '.join('?' * len(spaces))}) "
                           "AND entry_id = ?", (*spaces, entry_id))
        return rows[0] if rows else None

    def space_tag(self, session_id: str, space_id: str, shortest: int = 4) -> str:
        others = [row["id"] for row in self._query(
            "SELECT id FROM sessions WHERE memory_space = ? AND parent_id IS NULL AND id != ?",
            (space_id, session_id))]
        for length in range(shortest, len(session_id)):
            tag = session_id[:length]
            if not any(other.startswith(tag) for other in others):
                return tag
        return session_id

    def model_calls(self, session_id: str) -> list[dict]:
        rows = self._query("SELECT * FROM model_calls WHERE session_id = ? ORDER BY seq", (session_id,))
        return [{**row, "prefix": json.loads(row["prefix"])} for row in rows]

    def projects(self, limit: int | None = None) -> list[dict]:
        rows = self._query(
            "SELECT workspace_key, "
            "(SELECT workspace FROM sessions latest WHERE latest.workspace_key = s.workspace_key "
            "ORDER BY started_at DESC LIMIT 1) AS path, "
            "COUNT(*) AS sessions, SUM(status = 'running') AS running, "
            "MAX(MAX(started_at, COALESCE(ended_at, started_at))) AS last_active "
            "FROM sessions s WHERE parent_id IS NULL GROUP BY workspace_key ORDER BY last_active DESC LIMIT ?",
            (-1 if limit is None else limit,))
        return [{"path": row["path"], "name": Path(row["path"]).name, "sessions": row["sessions"],
                 "running": row["running"], "last_active": row["last_active"]} for row in rows]

    def sessions(self, project: str | None = None, limit: int | None = None) -> list[dict]:
        sql = ("SELECT id AS session_id, parent_id, workspace, task, mode, permission_mode, model_key, status, "
               "memory_space, started_at, ended_at FROM sessions")
        params: list = []
        if project is not None:
            sql += " WHERE workspace_key = ?"
            params.append(workspace_key(project))
        params.append(-1 if limit is None else limit)
        rows = self._query(sql + " ORDER BY started_at DESC LIMIT ?", tuple(params))
        return [{**row, "shared_memory": is_project_space(row["memory_space"])} for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
