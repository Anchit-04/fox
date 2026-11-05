import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from store import Store, project_space

out = sys.stdout.buffer
lock = threading.Lock()


def send(message: dict) -> None:
    with lock:
        out.write((json.dumps(message) + "\n").encode("utf-8"))
        out.flush()


def main() -> None:
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
    live: set[str] = set()

    def emit(session_id: str, event_type: str, **payload) -> None:
        seq = store.append_event(session_id, event_type, payload)
        send({"kind": "event", "event": {"session_id": session_id, "seq": seq, "type": event_type, **payload}})

    out.write(b"noise before ready\n")
    send({"kind": "ready", "pid": os.getpid()})
    for raw in sys.stdin.buffer:
        command = json.loads(raw)
        kind, session_id = command["kind"], command.get("session_id")
        if kind == "start_session":
            live.add(session_id)
            emit(session_id, "session_started", task=command["task"], workspace=args.workspace,
                 mode=command["mode"], permission_mode=command["permission_mode"],
                 model_key=command["model_key"], parent_id=command["parent_session_id"],
                 memory_space=project_space(args.workspace) if command.get("shared_memory") else session_id)
            emit(session_id, "agent_turn", task_id=None, model_key="fake", text=f"pid {os.getpid()}")
            emit(session_id, "awaiting_input", task_id=None)
        elif kind == "human_message":
            content = command["content"]
            if content == "crash":
                os._exit(3)
            if content == "noise":
                out.write(b"this line is not json\n")
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
                live.discard(session)
                status, text = ("done", "bye") if kind == "end_session" else ("failed", "interrupted: server stopped")
                emit(session, "session_ended", task_id=None, status=status, final_text=text)
            if not live:
                send({"kind": "exiting", "code": 0})
                return
    for session in list(live):
        emit(session, "session_ended", task_id=None, status="failed", final_text="interrupted: server stopped")


if __name__ == "__main__":
    main()
