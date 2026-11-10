import os
import sys

_PROTOCOL = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1, newline="\n")
_COMMANDS = os.fdopen(os.dup(0), "rb")
os.dup2(2, 1)
sys.stdout = sys.stderr

import argparse
import json
import threading
import time
from pathlib import Path

import agent
import baselines
import memory
import paths
import permissions
import resume
import transport
from approval import ApprovalGate
from history import History
from providers import Turn
from config import routing, vault
from injection import InjectionQueue
from orchestration.orchestrator import run_orchestrator
from store import Store, is_project_space
from store.history_log import Journal
from tools import browser_tools

SHUTDOWN_GRACE_SECONDS = 10
INTERRUPTED = "interrupted: server stopped"
PAUSED = "paused: stopped by you"
IDLE_PAUSE = "paused: idle"
REOPEN_WAIT_SECONDS = 5
IDLE_SECONDS = float(os.environ.get("FOX_WORKER_IDLE_SECONDS", 30 * 60))
WATCH_SECONDS = min(15.0, IDLE_SECONDS / 2)

_write_lock = threading.Lock()
_channel: "transport.WorkerChannel | None" = None


def send(message: dict) -> None:
    if _channel is not None:
        _channel.send(message)
        return
    line = json.dumps(message, ensure_ascii=False, default=str)
    with _write_lock:
        try:
            _PROTOCOL.write(line + "\n")
            _PROTOCOL.flush()
        except (OSError, ValueError):
            pass


class Session:
    def __init__(self, store: Store, session_id: str):
        self.store = store
        self.session_id = session_id
        self.injection_queue = InjectionQueue()
        self.approval_gate = ApprovalGate(emit=self.handle_event)
        self.mem: memory.Memory | None = None
        self.thread: threading.Thread | None = None
        self.ended = threading.Event()
        self.stop_reason: str | None = None
        self.idle_since: float | None = None
        self.reopen = None
        self.journal = Journal(store, session_id)

    def handle_event(self, event_type: str, payload: dict) -> None:
        if event_type == baselines.EVENT:
            self.store.put_baseline(self.session_id, payload["path"], payload["existed"], payload["content"])
            return
        if payload.get("task_id") is None:
            self.idle_since = time.monotonic() if event_type == "awaiting_input" else None
        seq = self.store.append_event(self.session_id, event_type, payload)
        send({"kind": "event", "event": {"session_id": self.session_id, "seq": seq, "type": event_type, **payload}})

    def end(self, task_id: str | None = None) -> None:
        self.injection_queue.close(task_id)
        self.approval_gate.close()


def side_conversation_task(store: Store, question: str, parent: Session) -> str:
    """Wrap a side question in what the main thread has established.

    Deliberately not the parent's transcript: that would be resent in full on
    every turn of the side conversation, which free-tier token budgets can't
    absorb. The todo list and the memory index are already externalised
    summaries of the same progress, so they carry the useful part cheaply.
    The shared Memory instance goes along too, so memory_read can pull a full
    entry on demand rather than everything being force-fed up front."""
    parts = ["[Side conversation. The main task is still running — you are not working on it.",
             "Answer the question; don't continue the main task unless asked.]", ""]
    row = store.session(parent.session_id)
    main_task, todos = row["task"], row["todos"]
    parts.append(f"Main task: {main_task}")
    if todos:
        parts.append(f"\nProgress so far:\n{todos}")
    if parent.mem is not None:
        index = parent.mem.read_index(limit=memory.INDEX_LIMIT)
        if index and index != "(empty)":
            parts.append(f"\nShared memory index (pull a full entry with memory_read):\n{index}")
    parts.append(f"\nQuestion: {question}")
    return "\n".join(parts)


def run_session(session: Session, task: str, model_key: str | None, mode: str, permission_mode: str,
                notes_seen: int = 0, resumed: "resume.Resumed | None" = None) -> None:
    status = "done"
    history = registry = None
    if resumed is not None:
        history = History(turns=resumed.turns, recorder=session.journal.recorder(None))
        history.add(Turn(role="user", text=resumed.note))
        registry = resume.restore_registry(session.store, session.session_id) if mode == "orchestrator" else None
    try:
        if mode == "orchestrator":
            final = run_orchestrator(
                task, model_key=model_key,
                event_sink=session.handle_event, mem=session.mem,
                injection_queue=session.injection_queue,
                approver=session.approval_gate, mode=permission_mode, notes_seen=notes_seen,
                journal=session.journal, history=history, registry=registry,
                wait_first=bool(resumed and resumed.waiting),
                on_reopen=lambda reopen: setattr(session, "reopen", reopen),
            )
        else:
            final = agent.run_agent(
                task, verbose=False, model_key=model_key or routing.pick_chat(), interactive=False,
                task_id=None, event_sink=session.handle_event, mem=session.mem,
                injection_queue=session.injection_queue, keep_alive=True,
                approver=session.approval_gate, mode=permission_mode,
                recorder=session.journal.recorder(None), history=history,
                wait_first=bool(resumed and resumed.waiting),
            )
    except Exception as e:
        status, final = "failed", str(e)
    if session.stop_reason:
        status = "paused" if session.stop_reason.startswith("paused") else "interrupted"
        final = session.stop_reason
    session.approval_gate.close()
    session.handle_event("session_ended", {"task_id": None, "status": status, "final_text": final})
    session.ended.set()


class Worker:
    def __init__(self, store: Store):
        self.store = store
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.commands = {
            "start_session": self.start_session,
            "human_message": self.human_message,
            "approval_response": self.approval_response,
            "end_session": self.end_session,
            "reload_keys": lambda command: vault.reload(),
            "shutdown": self.shutdown,
        }
        self.stop_reason = INTERRUPTED

    def live(self, session_id: str | None) -> Session | None:
        with self.lock:
            session = self.sessions.get(session_id)
        return session if session is not None and not session.ended.is_set() else None

    def resume_session(self, command: dict) -> None:
        session_id = command["session_id"]
        row = self.store.session(session_id)
        if row is None or row["parent_id"] is not None:
            send({"kind": "error", "session_id": session_id, "message": f"cannot resume {session_id!r}"})
            return
        vault.remember(command.get("validated") or {})
        session = Session(self.store, session_id)
        session.mem = memory.Memory.for_chat(self.store, session_id, row["workspace"],
                                             is_project_space(row["memory_space"]))
        resumed = resume.prepare(self.store, session_id, session.journal, session.handle_event)
        if resumed is None:
            session.handle_event("session_ended", {"task_id": None, "status": "failed",
                                                   "final_text": "nothing to resume: this chat has no stored history"})
            return
        model_key = command.get("model_key") or row["model_key"]
        with self.lock:
            self.sessions[session_id] = session
        session.handle_event("session_resumed", {"task_id": None, "reason": command.get("reason") or "resumed",
                                                 "model_key": model_key})
        notes_seen = session.journal.meta("notes_seen", None, 0)
        session.thread = threading.Thread(target=self._run, daemon=True,
                                          args=(session, row["task"], model_key, row["mode"],
                                                row["permission_mode"], notes_seen, resumed))
        session.thread.start()

    def start_session(self, command: dict) -> None:
        if command.get("resume"):
            self.resume_session(command)
            return
        session_id, parent_id = command["session_id"], command.get("parent_session_id")
        parent = self.live(parent_id) if parent_id else None
        if parent_id and parent is None:
            send({"kind": "error", "session_id": session_id, "message": f"unknown parent_session_id {parent_id!r}"})
            return
        vault.remember(command.get("validated") or {})
        task = side_conversation_task(self.store, command["task"], parent) if parent else command["task"]
        mode = command.get("mode") or "agent"
        permission_mode = command.get("permission_mode") or permissions.WORKSPACE_WRITE
        shared = parent is None and bool(command.get("shared_memory"))
        session = Session(self.store, session_id)
        session.mem = parent.mem if parent is not None else memory.Memory.for_chat(
            self.store, session_id, paths.workspace(), shared)
        with self.lock:
            self.sessions[session_id] = session
        session.handle_event("session_started", {
            "task": task, "workspace": str(paths.workspace()), "mode": mode, "permission_mode": permission_mode,
            "model_key": command.get("model_key"), "parent_id": parent_id, "memory_space": session.mem.write_space,
        })
        notes_seen, index = session.mem.join() if shared else (0, "")
        prompt = f"{task}\n\n{index}" if index else task
        session.thread = threading.Thread(target=self._run, daemon=True,
                                          args=(session, prompt, command.get("model_key"), mode, permission_mode,
                                                notes_seen))
        session.thread.start()

    def _run(self, session: Session, *args) -> None:
        run_session(session, *args)
        with self.lock:
            if all(s.ended.is_set() for s in self.sessions.values()):
                self.finished.set()

    def human_message(self, command: dict) -> None:
        session = self.live(command.get("session_id"))
        if session is None:
            send({"kind": "error", "session_id": command.get("session_id"),
                  "message": f"unknown session_id {command.get('session_id')!r}"})
            return
        task_id = command.get("task_id")
        if task_id is not None and self._finished(session, task_id):
            deadline = time.monotonic() + REOPEN_WAIT_SECONDS
            while session.reopen is None and time.monotonic() < deadline and not session.ended.is_set():
                time.sleep(0.05)
        if task_id is not None and session.reopen is not None and self._finished(session, task_id):
            refused = session.reopen(task_id, command["content"])
            if refused:
                send({"kind": "error", "session_id": session.session_id, "message": refused})
            return
        session.injection_queue.push(task_id, command["content"])

    def _finished(self, session: Session, task_id: str) -> bool:
        rows = self.store._query("SELECT status FROM tasks WHERE session_id = ? AND task_id = ?",
                                 (session.session_id, task_id))
        return bool(rows) and rows[0]["status"] in ("done", "failed", "cancelled", "escalated")

    def approval_response(self, command: dict) -> None:
        session = self.live(command.get("session_id"))
        if session is not None:
            session.approval_gate.respond(command["request_id"], bool(command.get("approved")),
                                          always=bool(command.get("always")), tool_name=command.get("tool_name"))

    def end_session(self, command: dict) -> None:
        session = self.live(command.get("session_id"))
        if session is not None:
            session.end(command.get("task_id"))

    def handle(self, line: str) -> None:
        try:
            command = json.loads(line)
            handler = self.commands[command["kind"]]
        except (ValueError, KeyError, TypeError):
            send({"kind": "error", "message": f"malformed command: {line.strip()[:200]}"})
            return
        try:
            handler(command)
        except Exception as e:
            send({"kind": "error", "session_id": command.get("session_id"), "message": f"{command['kind']} failed: {e}"})

    def shutdown(self, command: dict) -> None:
        if command.get("reason") == "paused":
            self.stop_reason = PAUSED
        self.finished.set()

    def hosted(self) -> list[dict]:
        with self.lock:
            sessions = list(self.sessions.values())
        return [{"session_id": s.session_id, "idle": s.idle_since is not None}
                for s in sessions if not s.ended.is_set()]

    def watch_idle(self) -> None:
        while not self.finished.wait(WATCH_SECONDS):
            with self.lock:
                live = [s for s in self.sessions.values() if not s.ended.is_set()]
            now = time.monotonic()
            if live and all(s.idle_since is not None and now - s.idle_since >= IDLE_SECONDS for s in live):
                self.stop_reason = IDLE_PAUSE
                self.finished.set()

    def read_commands(self, stream) -> None:
        for raw in stream:
            line = raw.decode("utf-8", errors="replace")
            if line.strip():
                self.handle(line)
        self.finished.set()

    def stop(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            if not session.ended.is_set():
                session.stop_reason = self.stop_reason
                session.end()
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        for session in sessions:
            if session.thread is not None:
                session.thread.join(max(0.0, deadline - time.monotonic()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fox session worker. Started by the hub, one per root session.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--db", type=Path, default=paths.db_path())
    parser.add_argument("--allow-unsafe-workspace", action="store_true")
    args = parser.parse_args(argv)
    paths.set_workspace(args.workspace, allow_unsafe=args.allow_unsafe_workspace)
    browser_tools.hub_owns_browser()
    store = Store.open(args.db, migrate=False)
    worker = Worker(store)
    token = os.environ.get(transport.TOKEN_ENV)
    if not token:
        threading.Thread(target=worker.read_commands, args=(_COMMANDS,), daemon=True).start()
        send({"kind": "ready", "pid": os.getpid()})
        worker.finished.wait()
        worker.stop()
        send({"kind": "exiting", "code": 0})
        return 0
    return serve_socket(worker, store, token, os.environ.get(transport.ROOT_ENV) or "", args.workspace)


def serve_socket(worker: "Worker", store: Store, token: str, root_id: str, workspace: str) -> int:
    global _channel
    _channel = transport.WorkerChannel(token)
    _channel.start(worker.handle, lambda: {"kind": "ready", "pid": os.getpid(), "sessions": worker.hosted()})
    store.register_worker(root_id, os.getpid(), _channel.port, token, workspace)
    threading.Thread(target=worker.watch_idle, daemon=True).start()
    try:
        worker.finished.wait()
        worker.stop()
        send({"kind": "exiting", "code": 0})
    finally:
        store.unregister_worker(root_id, os.getpid())
        _channel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
