import json
import sqlite3

from orchestration.tasks import TRANSITIONS

FINAL_STATUSES = frozenset(status.value for status, after in TRANSITIONS.items() if not after)


def _session_started(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO sessions (id, parent_id, workspace, task, mode, permission_mode, model_key, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, payload.get("parent_id"), payload["workspace"], payload["task"], payload["mode"],
         payload["permission_mode"], payload.get("model_key"), ts),
    )


def _session_ended(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute(
        "UPDATE sessions SET status = ?, final_text = ?, ended_at = ? WHERE id = ?",
        (payload["status"], payload.get("final_text"), ts, session_id),
    )


def _agent_turn(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    if payload.get("task_id") is None and payload.get("model_key"):
        db.execute("UPDATE sessions SET model_key = ? WHERE id = ?", (payload["model_key"], session_id))


def _todo_updated(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    if payload.get("task_id") is None:
        db.execute("UPDATE sessions SET todos = ? WHERE id = ?", (payload.get("checklist") or "", session_id))


def _task_created(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO tasks (session_id, task_id, description, lane, scope, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
        (session_id, payload["task_id"], payload["description"], payload["lane"],
         json.dumps(payload.get("scope") or []), ts),
    )


def _task_status_changed(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    status = payload["status"]
    columns, values = ["status = ?"], [status]
    for column in ("error", "model_key"):
        if payload.get(column) is not None:
            columns.append(f"{column} = ?")
            values.append(payload[column])
    if payload.get("report") is not None:
        columns.append("report = ?")
        values.append(json.dumps(payload["report"], default=str))
    if status == "running":
        columns.append("started_at = COALESCE(started_at, ?)")
        values.append(ts)
    if status in FINAL_STATUSES:
        columns.append("ended_at = ?")
        values.append(ts)
    db.execute(f"UPDATE tasks SET {', '.join(columns)} WHERE session_id = ? AND task_id = ?",
               (*values, session_id, payload["task_id"]))


def _memory_written(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO memory_entries (session_id, entry_id, task_id, agent, kind, scope, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, payload["entry_id"], payload.get("task_id"), payload["agent"], payload["kind"],
         json.dumps(payload.get("scope") or []), payload["content"], ts),
    )


HANDLERS = {
    "session_started": _session_started,
    "session_ended": _session_ended,
    "agent_turn": _agent_turn,
    "todo_updated": _todo_updated,
    "task_created": _task_created,
    "task_status_changed": _task_status_changed,
    "memory_written": _memory_written,
}


def apply(db: sqlite3.Connection, session_id: str, seq: int, ts: str, event_type: str, payload: dict) -> None:
    handler = HANDLERS.get(event_type)
    if handler is not None:
        handler(db, session_id, seq, ts, payload)
