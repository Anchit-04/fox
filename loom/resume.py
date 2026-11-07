import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import transport
from orchestration.tasks import Lane, Report, Task, TaskRegistry, TaskStatus
from providers import Turn

UNFINISHED = ("queued", "running", "needs-approval")
STOPPED_ERROR = "interrupted: the worker stopped before this task finished"
GIT_TIMEOUT_SECONDS = 10
MAX_GIT_LINES = 40
URL_LINE = re.compile(r"^URL: (\S+)", re.MULTILINE)


@dataclass
class Resumed:
    turns: list[Turn]
    waiting: bool
    stopped: list[dict]
    note: str


def was_waiting(turns: list[Turn]) -> bool:
    return bool(turns) and turns[-1].role == "assistant" and not turns[-1].tool_calls


def stop_unfinished_tasks(store, session_id: str, emit) -> list[dict]:
    rows = store._query("SELECT task_id, description, status FROM tasks WHERE session_id = ? AND status IN "
                        f"({', '.join('?' * len(UNFINISHED))}) ORDER BY created_at", (session_id, *UNFINISHED))
    for row in rows:
        emit("task_status_changed", {"task_id": row["task_id"], "status": "failed", "error": STOPPED_ERROR,
                                     "report": None, "model_key": None})
    return rows


def restore_registry(store, session_id: str) -> TaskRegistry:
    registry = TaskRegistry()
    for row in store._query("SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at", (session_id,)):
        report = json.loads(row["report"]) if row.get("report") else None
        registry.restore(Task(id=row["task_id"], description=row["description"], lane=Lane(row["lane"]),
                              scope=json.loads(row["scope"] or "[]"), model_key=row.get("model_key"),
                              status=TaskStatus(row["status"]),
                              report=Report(**report) if report else None, error=row.get("error"),
                              run=row.get("run") or 1))
    return registry


def last_url(turns: list[Turn]) -> str | None:
    for turn in reversed(turns):
        for result in reversed(turn.tool_results):
            if result.name.startswith("browser_"):
                found = URL_LINE.findall(result.content)
                if found:
                    return found[-1]
    return None


def git_lines(workspace: str | Path, *args: str) -> list[str] | None:
    try:
        done = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SECONDS, **transport.hidden())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return [line for line in done.stdout.splitlines() if line.strip()]


def resume_note(*, stopped_at: str, reason: str, stopped: list[dict], workspace: str | Path,
                url: str | None) -> str:
    lines = [f"[Fox resumed this chat] It stopped at {stopped_at} ({reason})."]
    if stopped:
        names = ", ".join(f'{row["task_id"]} "{row["description"][:80]}"' for row in stopped)
        lines.append(f"These tasks were running and were stopped: {names}.")
    status = git_lines(workspace, "status", "--short")
    if status is not None:
        if status:
            lines.append("Files changed in the workspace (git status --short):")
            lines += status[:MAX_GIT_LINES]
            if len(status) > MAX_GIT_LINES:
                lines.append(f"... and {len(status) - MAX_GIT_LINES} more")
            stat = git_lines(workspace, "diff", "--stat")
            if stat:
                lines.append("git diff --stat:")
                lines += stat[-MAX_GIT_LINES:]
        else:
            lines.append("git status shows no uncommitted changes.")
    if url:
        lines.append(f"Your browser page was {url}. Call browser_open to reopen it if you still need it.")
    lines.append("Check the current state before redoing anything.")
    return "\n".join(lines)


def prepare(store, session_id: str, journal, emit) -> Resumed | None:
    turns = journal.turns(None)
    if not turns:
        return None
    row = store.session(session_id) or {}
    events = store.events(session_id)
    stopped_at = (row.get("ended_at") or (events[-1]["ts"] if events else "an unknown time"))
    reason = row.get("final_text") or "the worker stopped"
    stopped = stop_unfinished_tasks(store, session_id, emit)
    note = resume_note(stopped_at=stopped_at, reason=reason, stopped=stopped,
                       workspace=row.get("workspace") or ".", url=last_url(turns))
    return Resumed(turns=turns, waiting=was_waiting(turns), stopped=stopped, note=note)
