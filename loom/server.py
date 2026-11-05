import asyncio
import json
import sys
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import websockets

import paths
import permissions
from config import vault
from store import Store

WORKER_SCRIPT = Path(__file__).resolve().with_name("worker.py")
READ_LIMIT = 64 * 1024 * 1024
READY_TIMEOUT_SECONDS = 30
SHUTDOWN_GRACE_SECONDS = 10
LOG_TAIL_LINES = 20
IDLE_EVENTS = {"awaiting_input", "model_error"}


class WorkerGone(Exception):
    pass


@dataclass
class SessionInfo:
    session_id: str
    parent_id: str | None
    workspace: str
    task: str
    mode: str
    permission_mode: str
    model_key: str | None
    shared_memory: bool = False
    status: str = "running"


class WorkerHandle:
    def __init__(self, root_id: str, workspace: str, log_path: Path):
        self.root_id = root_id
        self.workspace = workspace
        self.log_path = log_path
        self.process: asyncio.subprocess.Process | None = None
        self.sessions: set[str] = set()
        self.ended: set[str] = set()
        self.alive = False
        self.reason: str | None = None
        self.ready = asyncio.Event()
        self.exited = asyncio.Event()
        self.write_lock = asyncio.Lock()

    def hosts(self, session_id: str) -> bool:
        return self.alive and session_id in self.sessions and session_id not in self.ended

    def log_tail(self) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-LOG_TAIL_LINES:])


def session_status(event_type: str, payload: dict, current: str) -> str:
    if event_type == "session_ended":
        return payload.get("status") or "failed"
    if event_type == "approval_requested":
        return "needs_approval"
    if event_type in IDLE_EVENTS and payload.get("task_id") is None:
        return "idle"
    return "running" if current in ("idle", "needs_approval", "running") else current


class Hub:
    def __init__(self, store: Store, *, db_path: Path, default_workspace: Path, log_dir: Path,
                 allow_unsafe: bool = False, worker_command: list[str] | None = None):
        self.store = store
        self.db_path = db_path
        self.default_workspace = default_workspace
        self.log_dir = log_dir
        self.allow_unsafe = allow_unsafe
        self.worker_command = worker_command or [sys.executable, str(WORKER_SCRIPT)]
        self.workers: dict[str, WorkerHandle] = {}
        self.sessions: dict[str, SessionInfo] = {}
        self.subscribers: dict[str, set] = defaultdict(set)
        self.clients: set = set()
        self.background: set[asyncio.Task] = set()
        self.commands = {
            "start_session": self.start_session,
            "attach_session": self.attach_session,
            "human_message": self.human_message,
            "approval_response": self.approval_response,
            "end_session": self.end_session,
            "kill_session": self.kill_session,
            "list_models": self.list_models,
            "set_model_key": self.set_model_key,
            "list_projects": self.list_projects,
            "list_sessions": self.list_sessions,
        }

    def mark_interrupted(self) -> list[str]:
        orphans = [row["session_id"] for row in self.store.sessions() if row["status"] == "running"]
        for session_id in orphans:
            self.store.append_event(session_id, "session_ended", {
                "task_id": None, "status": "failed", "final_text": "interrupted: server restarted"})
        return orphans

    async def handler(self, ws) -> None:
        self.clients.add(ws)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    command = self.commands.get(msg.get("type"))
                except (ValueError, AttributeError):
                    await _reply(ws, {"type": "error", "message": "malformed message"})
                    continue
                if command is None:
                    await _reply(ws, {"type": "error", "message": f"unknown message type {msg.get('type')!r}"})
                    continue
                try:
                    await command(ws, msg)
                except KeyError as e:
                    await _reply(ws, {"type": "error", "message": f"{msg.get('type')} is missing {e}"})
        finally:
            self.clients.discard(ws)
            for watchers in self.subscribers.values():
                watchers.discard(ws)

    def _spawn_in_background(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def _live_worker(self, session_id: str | None) -> WorkerHandle | None:
        worker = self.workers.get(session_id)
        return worker if worker is not None and session_id not in worker.ended else None

    async def start_session(self, ws, msg: dict) -> None:
        permission_mode = msg.get("permission_mode") or permissions.WORKSPACE_WRITE
        if permission_mode not in permissions.MODES:
            await _reply(ws, {"type": "error", "message": f"unknown permission_mode {permission_mode!r}; "
                                                          f"expected one of {list(permissions.MODES)}"})
            return
        parent_id = msg.get("parent_session_id")
        session_id = str(uuid.uuid4())
        shared = bool(msg.get("shared_memory"))
        if parent_id:
            worker = self._live_worker(parent_id)
            if worker is None:
                await _reply(ws, {"type": "error", "message": f"unknown parent_session_id {parent_id!r}"})
                return
            shared = self.sessions[parent_id].shared_memory if parent_id in self.sessions else False
        else:
            try:
                workspace = paths.validate_workspace(msg.get("workspace") or self.default_workspace,
                                                     self.allow_unsafe)
            except paths.WorkspaceError as e:
                await _reply(ws, {"type": "error", "message": str(e)})
                return
            worker = WorkerHandle(session_id, str(workspace), self.log_dir / f"worker-{session_id}.log")

        info = SessionInfo(session_id=session_id, parent_id=parent_id, workspace=worker.workspace, task=msg["task"],
                           mode=msg.get("mode") or "agent", permission_mode=permission_mode,
                           model_key=msg.get("model_key"), shared_memory=shared)
        self.sessions[session_id] = info
        self.workers[session_id] = worker
        worker.sessions.add(session_id)
        self.subscribers[session_id].add(ws)
        await _reply(ws, {"type": "session_started", "session_id": session_id, "workspace": worker.workspace,
                          "shared_memory": shared})
        self._broadcast_status(session_id)

        command = {"kind": "start_session", "session_id": session_id, "task": info.task,
                   "model_key": info.model_key, "mode": info.mode, "permission_mode": permission_mode,
                   "parent_session_id": parent_id, "shared_memory": shared and not parent_id,
                   "validated": {e.model_key: e.validated for e in vault.list_models() if e.validated is not None}}
        if parent_id:
            try:
                await self._to_worker(worker, command)
            except WorkerGone:
                await self._record_failure(session_id, "worker exited before the side chat started")
        else:
            self._spawn_in_background(self._start_worker(worker, command))

    async def _start_worker(self, worker: WorkerHandle, command: dict) -> None:
        try:
            await self._launch(worker)
        except Exception as e:
            await self._start_failed(worker, str(e))
            return
        worker.alive = True
        async with worker.write_lock:
            try:
                await self._write(worker, command)
            except WorkerGone:
                pass
        worker.ready.set()
        self._spawn_in_background(self._relay(worker))

    async def _launch(self, worker: WorkerHandle) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        extra = ["--allow-unsafe-workspace"] if self.allow_unsafe else []
        with open(worker.log_path, "ab") as log:
            worker.process = await asyncio.create_subprocess_exec(
                *self.worker_command, "--workspace", worker.workspace, "--db", str(self.db_path), *extra,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log,
                cwd=worker.workspace, limit=READ_LIMIT)
        try:
            await asyncio.wait_for(self._wait_ready(worker), READY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise RuntimeError(f"no ready line within {READY_TIMEOUT_SECONDS}s") from None

    async def _wait_ready(self, worker: WorkerHandle) -> None:
        while True:
            line = await worker.process.stdout.readline()
            if not line:
                raise RuntimeError(f"exited with code {await worker.process.wait()}")
            try:
                if json.loads(line).get("kind") == "ready":
                    return
            except (ValueError, AttributeError):
                continue

    async def _start_failed(self, worker: WorkerHandle, reason: str) -> None:
        if worker.process is not None and worker.process.returncode is None:
            worker.process.kill()
            await worker.process.wait()
        text = f"worker failed to start: {reason}\n{worker.log_tail()}".strip()
        for session_id in sorted(worker.sessions):
            self._send_to_watchers(session_id, {"type": "error", "session_id": session_id, "message": text})
            await self._record_failure(session_id, text)
        self._forget(worker)
        worker.ready.set()
        worker.exited.set()

    async def _write(self, worker: WorkerHandle, command: dict) -> None:
        try:
            worker.process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))
            await worker.process.stdin.drain()
        except (ConnectionError, RuntimeError, AttributeError) as e:
            raise WorkerGone(str(e)) from e

    async def _to_worker(self, worker: WorkerHandle, command: dict) -> None:
        await worker.ready.wait()
        if not worker.alive:
            raise WorkerGone(worker.root_id)
        async with worker.write_lock:
            await self._write(worker, command)

    async def _relay(self, worker: WorkerHandle) -> None:
        stdout = worker.process.stdout
        while True:
            try:
                line = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                worker.reason = "worker sent a line over the size limit"
                worker.process.kill()
                break
            if not line:
                break
            try:
                message = json.loads(line)
                kind = message.get("kind")
            except (ValueError, AttributeError):
                continue
            if kind == "event":
                await self._on_event(message["event"])
            elif kind == "error":
                session_id = message.get("session_id")
                self._send_to_watchers(session_id, {"type": "error", "session_id": session_id,
                                                    "message": message.get("message")})
        await self._worker_exited(worker, await worker.process.wait())

    async def _worker_exited(self, worker: WorkerHandle, code: int) -> None:
        worker.alive = False
        text = worker.reason or f"worker exited with code {code}"
        for session_id in sorted(worker.sessions - worker.ended):
            await self._record_failure(session_id, text)
        self._forget(worker)
        worker.exited.set()

    def _forget(self, worker: WorkerHandle) -> None:
        worker.alive = False
        for session_id in worker.sessions:
            if self.workers.get(session_id) is worker:
                del self.workers[session_id]

    async def _record_failure(self, session_id: str, text: str) -> None:
        info = self.sessions[session_id]
        if self.store.session(session_id) is None:
            await self._emit(session_id, "session_started", {
                "task": info.task, "workspace": info.workspace, "mode": info.mode,
                "permission_mode": info.permission_mode, "model_key": info.model_key, "parent_id": info.parent_id,
                "memory_space": None})
        await self._emit(session_id, "session_ended", {"task_id": None, "status": "failed", "final_text": text})

    async def _emit(self, session_id: str, event_type: str, payload: dict) -> None:
        seq = self.store.append_event(session_id, event_type, payload)
        await self._on_event({"session_id": session_id, "seq": seq, "type": event_type, **payload})

    async def _on_event(self, event: dict) -> None:
        session_id, event_type = event.get("session_id"), event.get("type")
        self._send_to_watchers(session_id, event)
        if event_type == "session_ended":
            worker = self.workers.get(session_id)
            if worker is not None:
                worker.ended.add(session_id)
        info = self.sessions.get(session_id)
        if info is None:
            return
        if event_type == "agent_turn" and event.get("task_id") is None and event.get("model_key"):
            info.model_key = event["model_key"]
        status = session_status(event_type, event, info.status)
        if status != info.status:
            info.status = status
            self._broadcast_status(session_id)

    def _send_to_watchers(self, session_id: str | None, message: dict) -> None:
        watchers = self.subscribers.get(session_id) if session_id else None
        if watchers:
            websockets.broadcast(watchers, json.dumps(message, ensure_ascii=False, default=str))

    def _broadcast_status(self, session_id: str) -> None:
        info = self.sessions[session_id]
        websockets.broadcast(self.clients, json.dumps({
            "type": "session_status", "session_id": session_id, "parent_id": info.parent_id,
            "workspace": info.workspace, "task": info.task, "status": info.status, "model_key": info.model_key,
            "mode": info.mode, "permission_mode": info.permission_mode, "shared_memory": info.shared_memory}))

    async def _forward(self, ws, msg: dict, command: dict) -> bool:
        session_id = msg.get("session_id")
        worker = self._live_worker(session_id)
        try:
            if worker is None:
                raise WorkerGone(session_id)
            await self._to_worker(worker, {**command, "session_id": session_id})
            return True
        except WorkerGone:
            await _reply(ws, {"type": "error", "message": f"unknown session_id {session_id!r}"})
            return False

    async def attach_session(self, ws, msg: dict) -> None:
        session_id = msg["session_id"]
        self.subscribers[session_id].add(ws)
        state = self.store.session_view(session_id)
        if state is None:
            self.subscribers[session_id].discard(ws)
            await _reply(ws, {"type": "error", "message": f"unknown session_id {session_id!r}"})
            return
        await _reply(ws, {"type": "attached", "session_id": session_id, "state": state})

    async def human_message(self, ws, msg: dict) -> None:
        command = {"kind": "human_message", "task_id": msg.get("task_id"), "content": msg["content"]}
        if await self._forward(ws, msg, command):
            await _reply(ws, {"type": "human_message_queued", "session_id": msg["session_id"],
                              "task_id": msg.get("task_id")})

    async def approval_response(self, ws, msg: dict) -> None:
        await self._forward(ws, msg, {"kind": "approval_response", "request_id": msg["request_id"],
                                      "approved": bool(msg.get("approved")), "always": bool(msg.get("always")),
                                      "tool_name": msg.get("tool_name")})

    async def end_session(self, ws, msg: dict) -> None:
        if await self._forward(ws, msg, {"kind": "end_session", "task_id": msg.get("task_id")}):
            await _reply(ws, {"type": "end_session_queued", "session_id": msg["session_id"]})

    async def kill_session(self, ws, msg: dict) -> None:
        session_id = msg.get("session_id")
        worker = self._live_worker(session_id)
        if worker is None:
            await _reply(ws, {"type": "error", "message": f"unknown session_id {session_id!r}"})
            return
        await worker.ready.wait()
        if worker.process is not None and worker.process.returncode is None:
            worker.reason = "killed"
            worker.process.kill()
        await worker.exited.wait()
        await _reply(ws, {"type": "kill_session_done", "session_id": session_id})

    async def list_models(self, ws, msg: dict) -> None:
        # validate=True re-authenticates every present key for real;
        # without it, `validated` is whatever the cache holds
        # (null = never checked), which is cheap but not proof.
        if msg.get("validate"):
            await asyncio.to_thread(_validate_all_keys)
        await _reply(ws, {"type": "models", "models": [asdict(e) for e in vault.list_models()]})

    async def set_model_key(self, ws, msg: dict) -> None:
        try:
            vault.set_key(msg["model_key"], msg["value"])
            await asyncio.to_thread(vault.validate_key, msg["model_key"], True)
        except Exception as e:
            await _reply(ws, {"type": "error", "message": f"could not save key: {e}"})
            return
        for worker in {w for w in self.workers.values() if w.alive}:
            try:
                await self._to_worker(worker, {"kind": "reload_keys"})
            except WorkerGone:
                pass
        await _reply(ws, {"type": "models", "models": [asdict(e) for e in vault.list_models()]})

    async def list_projects(self, ws, msg: dict) -> None:
        await _reply(ws, {"type": "projects", "current": str(self.default_workspace),
                          "projects": self.store.projects(msg.get("limit"))})

    async def list_sessions(self, ws, msg: dict) -> None:
        rows = self.store.sessions(msg.get("project"), msg.get("limit"))
        for row in rows:
            row["live"] = self._live_worker(row["session_id"]) is not None
            info = self.sessions.get(row["session_id"])
            if row["live"] and info is not None:
                row["status"] = info.status
        await _reply(ws, {"type": "sessions", "sessions": rows})

    async def shutdown(self) -> None:
        workers = {w for w in self.workers.values() if w.alive}
        for worker in workers:
            try:
                await asyncio.wait_for(self._to_worker(worker, {"kind": "shutdown"}), 1)
            except (WorkerGone, asyncio.TimeoutError):
                pass
        waiting = [asyncio.create_task(w.exited.wait()) for w in workers]
        if waiting:
            await asyncio.wait(waiting, timeout=SHUTDOWN_GRACE_SECONDS)
        for worker in workers:
            if not worker.exited.is_set() and worker.process.returncode is None:
                worker.reason = "interrupted: server stopped"
                worker.process.kill()
        if waiting:
            await asyncio.wait(waiting, timeout=5)


async def _reply(ws, message: dict) -> None:
    try:
        await ws.send(json.dumps(message, ensure_ascii=False, default=str))
    except websockets.ConnectionClosed:
        pass


def _validate_all_keys() -> None:
    for entry in vault.list_models():
        if entry.present:
            vault.validate_key(entry.model_key, force=True)


async def serve(hub: Hub, host: str, port: int) -> None:
    async with websockets.serve(hub.handler, host, port):
        print(f"server.py listening on ws://{host}:{port}")
        try:
            await asyncio.Future()
        finally:
            await hub.shutdown()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fox hub: starts one worker process per session.")
    parser.add_argument("--workspace", default=None,
                        help="Default folder for new sessions. Defaults to the current directory.")
    parser.add_argument("--allow-unsafe-workspace", action="store_true",
                        help="Permit a drive root or your home directory as a workspace.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    try:
        default_workspace = paths.validate_workspace(args.workspace or paths.workspace(),
                                                     allow_unsafe=args.allow_unsafe_workspace)
    except paths.WorkspaceError as e:
        raise SystemExit(f"error: {e}")

    hub_store = Store.open(paths.db_path())
    hub = Hub(hub_store, db_path=paths.db_path(), default_workspace=default_workspace,
              log_dir=paths.FOX_HOME / "logs", allow_unsafe=args.allow_unsafe_workspace)
    interrupted = hub.mark_interrupted()
    # Printed prominently because it's inferred from cwd when not passed, and
    # launching from the wrong directory is otherwise invisible until the agent
    # writes somewhere surprising.
    print(f"default workspace: {default_workspace}")
    print(f"fox home:          {paths.FOX_HOME}")
    if interrupted:
        print(f"marked {len(interrupted)} interrupted session(s) from the last run")
    try:
        asyncio.run(serve(hub, args.host, args.port))
    except KeyboardInterrupt:
        pass
