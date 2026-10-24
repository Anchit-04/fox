import argparse
import asyncio
import json
import re

import websockets

SAFE_TEST = re.compile(r"^(py|python|python3) -m (unittest|pytest)( [\w./\\-]+)*$")


def approve(msg: dict) -> bool:
    command = (msg.get("args") or {}).get("command", "").strip()
    return msg.get("tool_name") == "run_bash_command" and bool(SAFE_TEST.match(command))


def show(msg: dict) -> str | None:
    kind, tid = msg.get("type"), msg.get("task_id")
    if kind == "task_created":
        return f"  + {tid} [{msg['lane']}] scope={msg['scope']}  {msg['description'][:90]!r}"
    if kind == "task_status_changed":
        return f"  ~ {tid} -> {msg['status']}" + (f"  ({msg['error'][:100]})" if msg.get("error") else "")
    if kind == "agent_turn" and tid is None:
        return f"orchestrator: {msg['text'][:400]}"
    if kind == "proposal_made":
        return f"  ? {tid} proposes: {msg['text'][:120]}"
    if kind == "scope_audit":
        return (f"  # {tid} audit: declared={msg['declared']} written={msg['written']} "
                f"mispredicted={msg['mispredicted']}")
    if kind == "error":
        return f"  ! {msg.get('message')}"
    return None


async def run(task: str, url: str, model_key: str | None, permission_mode: str) -> str:
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start_session", "task": task, "mode": "orchestrator",
                                  "model_key": model_key, "permission_mode": permission_mode}))
        session_id = json.loads(await ws.recv())["session_id"]
        print(f"session {session_id}", flush=True)
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "approval_requested":
                ok = approve(msg)
                print(f"  {'APPROVED' if ok else 'DENIED  '} {msg.get('description')}", flush=True)
                await ws.send(json.dumps({"type": "approval_response", "session_id": session_id,
                                          "request_id": msg["request_id"], "approved": ok}))
                continue
            line = show(msg)
            if line:
                print(line, flush=True)
            if msg.get("type") == "session_ended":
                print(f"session ended: {msg['status']}\nfinal: {msg['final_text']}", flush=True)
                break
    return session_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one orchestrator session headlessly against a running server.")
    parser.add_argument("task")
    parser.add_argument("--url", default="ws://localhost:8765")
    parser.add_argument("--model-key", default=None)
    parser.add_argument("--permission-mode", default="workspace-write")
    args = parser.parse_args()
    asyncio.run(run(args.task, args.url, args.model_key, args.permission_mode))


if __name__ == "__main__":
    main()
