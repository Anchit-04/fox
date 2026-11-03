import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loom"))

import websockets
from websockets.asyncio.client import connect

import server
from store import Store

FAKE = [sys.executable, str(ROOT / "tests" / "fake_worker.py")]
WAIT = 15


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.seen: list[dict] = []

    async def send(self, **msg):
        await self.ws.send(json.dumps(msg))

    async def until(self, match, timeout=WAIT) -> dict:
        for message in self.seen:
            if match(message):
                self.seen.remove(message)
                return message
        async with asyncio.timeout(timeout):
            while True:
                message = json.loads(await self.ws.recv())
                if match(message):
                    return message
                self.seen.append(message)

    async def event(self, session_id, event_type, **fields) -> dict:
        return await self.until(lambda m: m.get("session_id") == session_id and m.get("type") == event_type
                                and all(m.get(k) == v for k, v in fields.items()))

    async def start(self, workspace=None, **extra) -> str:
        await self.send(type="start_session", task="do it", model_key="fake", mode="agent",
                        **({"workspace": str(workspace)} if workspace else {}), **extra)
        reply = await self.until(lambda m: m.get("type") in ("session_started", "error") and "seq" not in m)
        if reply["type"] == "error":
            raise AssertionError(reply["message"])
        return reply["session_id"]

    def pid(self, message) -> int:
        return int(message["text"].split()[1])


class HubCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.project_a, self.project_b = base / "shop", base / "api"
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.db = base / "fox.db"
        self.store = Store.open(self.db)
        self.hub = server.Hub(self.store, db_path=self.db, default_workspace=self.project_a, log_dir=base / "logs",
                              worker_command=FAKE)
        self.server = await websockets.serve(self.hub.handler, "localhost", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients: list[Client] = []

    async def asyncTearDown(self):
        await self.hub.shutdown()
        for client in self.clients:
            await client.ws.close()
        self.server.close()
        await self.server.wait_closed()
        self.store.close()

    async def client(self) -> Client:
        client = Client(await connect(f"ws://localhost:{self.port}", max_size=None))
        self.clients.append(client)
        return client


class StartingAndRouting(HubCase):
    async def test_start_replies_with_workspace_and_events_carry_seq(self):
        tui = await self.client()
        await tui.send(type="start_session", task="do it", model_key="fake", mode="agent")
        reply = await tui.until(lambda m: m.get("type") == "session_started")
        self.assertEqual(reply["workspace"], str(self.project_a.resolve()))
        turn = await tui.event(reply["session_id"], "agent_turn")
        self.assertGreater(turn["seq"], 1)

    async def test_events_go_to_watchers_and_status_goes_to_everyone(self):
        tui, other = await self.client(), await self.client()
        await other.send(type="list_projects")
        await other.until(lambda m: m.get("type") == "projects")
        session_id = await tui.start()
        await tui.event(session_id, "awaiting_input")
        idle = await other.until(lambda m: m.get("type") == "session_status" and m["status"] == "idle")
        self.assertEqual((idle["session_id"], idle["workspace"]), (session_id, str(self.project_a.resolve())))
        self.assertFalse([m for m in other.seen if m.get("type") == "agent_turn"])

    async def test_two_workers_and_messages_reach_the_right_one(self):
        tui = await self.client()
        a, b = await tui.start(self.project_a), await tui.start(self.project_b)
        pid_a = tui.pid(await tui.event(a, "agent_turn"))
        pid_b = tui.pid(await tui.event(b, "agent_turn"))
        self.assertNotEqual(pid_a, pid_b)
        await tui.send(type="human_message", session_id=b, content="hello b")
        await tui.until(lambda m: m.get("type") == "human_message_queued" and m["session_id"] == b)
        self.assertEqual((await tui.event(b, "agent_turn", text="echo hello b"))["session_id"], b)
        await tui.send(type="approval_response", session_id=a, request_id="r1", approved=True)
        await tui.event(a, "agent_turn", text="approved=True")

    async def test_a_side_chat_runs_in_its_parents_worker(self):
        tui = await self.client()
        parent = await tui.start()
        pid = tui.pid(await tui.event(parent, "agent_turn"))
        side = await tui.start(parent_session_id=parent)
        self.assertEqual(tui.pid(await tui.event(side, "agent_turn")), pid)
        self.assertEqual(self.store.session(side)["parent_id"], parent)

    async def test_bad_workspace_and_unknown_sessions_are_errors(self):
        tui = await self.client()
        with self.assertRaises(AssertionError):
            await tui.start(self.project_a / "missing")
        for msg in ({"type": "human_message", "session_id": "nope", "content": "x"},
                    {"type": "end_session", "session_id": "nope"},
                    {"type": "kill_session", "session_id": "nope"},
                    {"type": "start_session", "task": "x", "parent_session_id": "nope"}):
            await tui.send(**msg)
            error = await tui.until(lambda m: m.get("type") == "error")
            self.assertIn("nope", error["message"])

    async def test_end_session_ends_it_and_the_worker_exits(self):
        tui = await self.client()
        session_id = await tui.start()
        await tui.event(session_id, "awaiting_input")
        await tui.send(type="end_session", session_id=session_id)
        await tui.until(lambda m: m.get("type") == "end_session_queued")
        ended = await tui.event(session_id, "session_ended")
        self.assertEqual(ended["status"], "done")
        async with asyncio.timeout(WAIT):
            while self.hub._live_worker(session_id) is not None or self.hub.workers:
                await asyncio.sleep(0.05)


class Failures(HubCase):
    async def test_a_crashed_worker_fails_only_its_own_session(self):
        tui = await self.client()
        a, b = await tui.start(self.project_a), await tui.start(self.project_b)
        await tui.event(a, "awaiting_input")
        await tui.event(b, "awaiting_input")
        await tui.send(type="human_message", session_id=a, content="crash")
        ended = await tui.event(a, "session_ended")
        self.assertEqual((ended["status"], ended["final_text"]), ("failed", "worker exited with code 3"))
        self.assertEqual(self.store.session(a)["status"], "failed")
        await tui.send(type="human_message", session_id=b, content="still here?")
        await tui.event(b, "agent_turn", text="echo still here?")

    async def test_a_worker_that_never_gets_ready_fails_the_start(self):
        with mock.patch.dict(os.environ, {"FAKE_WORKER": "never_ready"}), \
                mock.patch.object(server, "READY_TIMEOUT_SECONDS", 1):
            tui = await self.client()
            session_id = await tui.start()
            error = await tui.event(session_id, "error")
            ended = await tui.event(session_id, "session_ended")
        self.assertIn("no ready line", error["message"])
        self.assertTrue(ended["final_text"].startswith("worker failed to start"))
        self.assertEqual(self.store.session(session_id)["status"], "failed")

    async def test_a_start_failure_reports_the_end_of_the_workers_log(self):
        with mock.patch.dict(os.environ, {"FAKE_WORKER": "die_at_start"}):
            tui = await self.client()
            session_id = await tui.start()
            ended = await tui.event(session_id, "session_ended")
        self.assertIn("exited with code 7", ended["final_text"])
        self.assertIn("fake worker could not start", ended["final_text"])

    async def test_kill_session_stops_the_worker(self):
        tui = await self.client()
        session_id = await tui.start()
        await tui.event(session_id, "awaiting_input")
        await tui.send(type="kill_session", session_id=session_id)
        await tui.until(lambda m: m.get("type") == "kill_session_done")
        self.assertEqual(self.store.session(session_id)["final_text"], "killed")

    async def test_startup_marks_sessions_left_running_as_interrupted(self):
        self.store.append_event("old", "session_started", {
            "task": "t", "workspace": str(self.project_a), "mode": "agent", "permission_mode": "workspace-write",
            "model_key": None, "parent_id": None})
        self.assertEqual(self.hub.mark_interrupted(), ["old"])
        row = self.store.session("old")
        self.assertEqual((row["status"], row["final_text"]), ("failed", "interrupted: server restarted"))
        self.assertEqual(len(self.store.session_view("old")["turns"]), 2)

    async def test_shutdown_stops_every_worker_and_records_the_interruption(self):
        tui = await self.client()
        a, b = await tui.start(self.project_a), await tui.start(self.project_b)
        await tui.event(a, "awaiting_input")
        await tui.event(b, "awaiting_input")
        await self.hub.shutdown()
        rows = [self.store.session(s) for s in (a, b)]
        self.assertEqual({(r["status"], r["final_text"]) for r in rows}, {("failed", "interrupted: server stopped")})
        self.assertEqual(self.hub.workers, {})


class Relay(HubCase):
    async def test_a_two_megabyte_event_and_junk_lines_survive_the_pipe(self):
        tui = await self.client()
        session_id = await tui.start()
        await tui.event(session_id, "awaiting_input")
        await tui.send(type="human_message", session_id=session_id, content="big")
        big = await tui.until(lambda m: m.get("type") == "agent_turn" and len(m.get("text", "")) > 1000)
        self.assertEqual(len(big["text"]), 2 * 1024 * 1024)
        await tui.send(type="human_message", session_id=session_id, content="noise")
        await tui.event(session_id, "agent_turn", text="echo noise")

    async def test_saving_a_key_tells_live_workers_to_reload(self):
        tui = await self.client()
        session_id = await tui.start()
        await tui.event(session_id, "awaiting_input")
        with mock.patch.object(server.vault, "set_key"), mock.patch.object(server.vault, "validate_key"):
            await tui.send(type="set_model_key", model_key="nemotron-super", value="not-a-real-key")
            await tui.until(lambda m: m.get("type") == "models")
        await tui.event(session_id, "agent_turn", text="keys reloaded")


class Listing(HubCase):
    async def test_projects_merge_spellings_and_sessions_know_what_is_live(self):
        tui = await self.client()
        live = await tui.start(self.project_a)
        await tui.event(live, "awaiting_input")
        self.store.append_event("older", "session_started", {
            "task": "t", "workspace": str(self.project_a).upper(), "mode": "agent",
            "permission_mode": "workspace-write", "model_key": None, "parent_id": None})
        self.store.append_event("older", "session_ended", {"task_id": None, "status": "done", "final_text": "ok"})
        await tui.send(type="list_projects", limit=5)
        projects = await tui.until(lambda m: m.get("type") == "projects")
        self.assertEqual(projects["current"], str(self.project_a))
        expected = 1 if sys.platform == "win32" else 2
        self.assertEqual(len(projects["projects"]), expected)
        await tui.send(type="list_sessions", project=str(self.project_a))
        sessions = (await tui.until(lambda m: m.get("type") == "sessions"))["sessions"]
        live_flags = {s["session_id"]: s["live"] for s in sessions}
        self.assertTrue(live_flags[live])
        if sys.platform == "win32":
            self.assertFalse(live_flags["older"])


class HubImports(unittest.TestCase):
    def test_the_hub_loads_no_agent_code_and_no_sdk(self):
        probe = ("import sys, server\n"
                 "names = ('agent', 'orchestration.orchestrator', 'memory', 'approval', 'injection', "
                 "'google.genai', 'openai', 'anthropic')\n"
                 "print(','.join(n for n in names if n in sys.modules))")
        out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT / "loom", capture_output=True, text=True,
                             check=True)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
