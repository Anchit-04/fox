import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import transport
from store import Store, project_space

out = sys.stdout.buffer
lock = threading.Lock()
channel: transport.WorkerChannel | None = None
commands: "queue.Queue[bytes | None]" = queue.Queue()
connected = threading.Event()


def send(message: dict) -> None:
    if channel is not None:
        channel.send(message)
        return
    with lock:
        out.write((json.dumps(message) + "\n").encode("utf-8"))
        out.flush()


def send_raw(data: bytes) -> None:
    if channel is not None:
        channel.send_raw(data)
    else:
        out.write(data)


def watch_connection() -> None:
    connected.wait()
    while True:
        time.sleep(0.1)
        if not channel.connected and not os.environ.get("FAKE_WORKER_SURVIVES"):
            commands.put(None)
            return


def main() -> None:
    global channel
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--allow-unsafe-workspace", action="store_true")
    args = parser.parse_args()
    if os.environ.get("FAKE_WORKER") == "never_ready":
        time.sleep(3600)
    if os.environ.get("FAKE_WORKER") == "die_at_start":
        print("fake worker could not start", file=sys.stderr)
        sys.exit(7)
    store = Store.open(args.db, migrate=False)
    live: dict[str, bool] = {}

    def emit(session_id: str, event_type: str, **payload) -> None:
        seq = store.append_event(session_id, event_type, payload)
        if payload.get("task_id") is None and session_id in live:
            live[session_id] = event_type == "awaiting_input"
        send({"kind": "event", "event": {"session_id": session_id, "seq": seq, "type": event_type, **payload}})

    token = os.environ.get(transport.TOKEN_ENV)
    root = os.environ.get(transport.ROOT_ENV) or ""
    out.write(b"noise before ready\n")
    if token:
        channel = transport.WorkerChannel(token)

        def hello() -> dict:
            connected.set()
            return {"kind": "ready", "pid": os.getpid(),
                    "sessions": [{"session_id": s, "idle": idle} for s, idle in live.items()]}

        channel.start(lambda line: commands.put(line.encode("utf-8")), hello)
        store.register_worker(root, os.getpid(), channel.port, token, args.workspace)
        threading.Thread(target=watch_connection, daemon=True).start()
        source = iter(commands.get, None)
    else:
        send({"kind": "ready", "pid": os.getpid()})
        source = sys.stdin.buffer
    try:
        for raw in source:
            command = json.loads(raw)
            kind, session_id = command["kind"], command.get("session_id")
            if kind == "start_session":
                live[session_id] = False
                if command.get("resume"):
                    emit(session_id, "session_resumed", task_id=None, reason=command.get("reason") or "resumed")
                    emit(session_id, "agent_turn", task_id=None, model_key="fake", text=f"resumed by {os.getpid()}")
                else:
                    emit(session_id, "session_started", task=command["task"], workspace=args.workspace,
                         mode=command["mode"], permission_mode=command["permission_mode"],
                         model_key=command["model_key"], parent_id=command["parent_session_id"],
                         memory_space=project_space(args.workspace) if command.get("shared_memory") else session_id)
                    emit(session_id, "agent_turn", task_id=None, model_key="fake", text=f"pid {os.getpid()}")
                if command.get("hold") or os.environ.get("FAKE_WORKER_HOLD"):
                    continue
                emit(session_id, "awaiting_input", task_id=None)
            elif kind == "human_message":
                content = command["content"]
                if content == "crash":
                    os._exit(3)
                if content == "noise":
                    send_raw(b"this line is not json\n")
                    print("stray print", file=sys.stderr)
                text = "x" * (2 * 1024 * 1024) if content == "big" else f"echo {content}"
                emit(session_id, "agent_turn", task_id=None, model_key="fake", text=text)
                emit(session_id, "awaiting_input", task_id=None)
            elif kind == "approval_response":
                emit(session_id, "agent_turn", task_id=None, model_key="fake", text=f"approved={command['approved']}")
            elif kind == "reload_keys":
                for session in live:
                    emit(session, "agent_turn", task_id=None, model_key="fake", text="keys reloaded")
            elif kind in ("end_session", "shutdown"):
                for session in [session_id] if kind == "end_session" else list(live):
                    live.pop(session, None)
                    if kind == "end_session":
                        status, text = "done", "bye"
                    elif command.get("reason") == "paused":
                        status, text = "paused", "paused: stopped by you"
                    else:
                        status, text = "interrupted", "interrupted: server stopped"
                    emit(session, "session_ended", task_id=None, status=status, final_text=text)
                if not live:
                    send({"kind": "exiting", "code": 0})
                    return
        for session in list(live):
            emit(session, "session_ended", task_id=None, status="interrupted", final_text="interrupted: server stopped")
    finally:
        if channel is not None:
            store.unregister_worker(root, os.getpid())
            channel.close()


if __name__ == "__main__":
    main()
