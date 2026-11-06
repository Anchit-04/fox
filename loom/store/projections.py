import json
import sqlite3

from orchestration.tasks import TRANSITIONS
from paths import workspace_key

FINAL_STATUSES = frozenset(status.value for status, after in TRANSITIONS.items() if not after)


def _session_started(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO sessions (id, parent_id, workspace, workspace_key, task, mode, permission_mode, model_key, "
        "memory_space, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, payload.get("parent_id"), payload["workspace"], workspace_key(payload["workspace"]),
         payload["task"], payload["mode"], payload["permission_mode"], payload.get("model_key"),
         payload.get("memory_space") or payload.get("parent_id") or session_id, ts),
    )


def _session_ended(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute(
        "UPDATE sessions SET status = ?, final_text = ?, ended_at = ? WHERE id = ?",
        (payload["status"], payload.get("final_text"), ts, session_id),
    )


def _session_resumed(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    columns, values = ["status = 'running'", "final_text = NULL", "ended_at = NULL"], []
    if payload.get("model_key"):
        columns.append("model_key = ?")
        values.append(payload["model_key"])
    db.execute(f"UPDATE sessions SET {', '.join(columns)} WHERE id = ?", (*values, session_id))


def _session_archived(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute("UPDATE sessions SET archived_at = ? WHERE id = ?",
               (ts if payload.get("archived", True) else None, session_id))


def _task_reopened(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute("UPDATE tasks SET status = 'running', run = run + 1, error = NULL, report = NULL, ended_at = NULL "
               "WHERE session_id = ? AND task_id = ?", (session_id, payload["task_id"]))


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
    space_id = payload.get("space_id") or session_id
    db.execute(
        "INSERT INTO memory_entries (space_id, session_id, entry_id, task_id, agent, kind, scope, content, "
        "created_at, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "(SELECT COALESCE(MAX(position), 0) + 1 FROM memory_entries WHERE space_id = ?))",
        (space_id, session_id, payload["entry_id"], payload.get("task_id"), payload["agent"], payload["kind"],
         json.dumps(payload.get("scope") or []), payload["content"], ts, space_id),
    )


def _model_called(db: sqlite3.Connection, session_id: str, seq: int, ts: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO model_calls (session_id, seq, task_id, model_key, duration_ms, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, cost, cost_source, prefix, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, seq, payload.get("task_id"), payload["model_key"], payload["duration_ms"],
         payload["input_tokens"], payload["output_tokens"], payload["cache_read_tokens"],
         payload["cache_write_tokens"], payload.get("cost"), payload.get("cost_source"),
         json.dumps(payload.get("prefix") or {}), ts),
    )


HANDLERS = {
    "session_started": _session_started,
    "session_ended": _session_ended,
    "session_resumed": _session_resumed,
    "task_reopened": _task_reopened,
    "session_archived": _session_archived,
    "agent_turn": _agent_turn,
    "todo_updated": _todo_updated,
    "task_created": _task_created,
    "task_status_changed": _task_status_changed,
    "memory_written": _memory_written,
    "model_called": _model_called,
}


def apply(db: sqlite3.Connection, session_id: str, seq: int, ts: str, event_type: str, payload: dict) -> None:
    handler = HANDLERS.get(event_type)
    if handler is not None:
        handler(db, session_id, seq, ts, payload)
