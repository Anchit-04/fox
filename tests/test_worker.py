import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loom"))

from store import Store, project_space

SCRIPTED = ROOT / "tests" / "scripted_worker.py"


class WorkerProcess(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.db = self.folder / "fox.db"
        Store.open(self.db).close()
        self.log = open(self.folder / "worker.log", "w", encoding="utf-8")
        self.addCleanup(self.log.close)
        started = time.perf_counter()
        self.proc = subprocess.Popen([sys.executable, str(SCRIPTED), "--workspace", str(self.folder), "--db",
                                      str(self.db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.log, cwd=self.folder)
        self.addCleanup(self._close)
        self.lines = []
        self.ready = self.read()
        self.ready_seconds = time.perf_counter() - started

    def _close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
        self.proc.stdin.close()
        self.proc.stdout.close()

    def read(self) -> dict:
        line = self.proc.stdout.readline()
        self.assertTrue(line, "worker closed its stdout")
        self.lines.append(line)
        return json.loads(line)

    def until(self, event_type: str) -> list[dict]:
        seen = []
        while True:
            message = self.read()
            if message["kind"] == "event":
                seen.append(message["event"])
                if message["event"]["type"] == event_type:
                    return seen

    def send(self, **command):
        self.proc.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def start(self, session_id="s1"):
        self.send(kind="start_session", session_id=session_id, task="what is this?", model_key="nemotron-super",
                  mode="agent", permission_mode="workspace-write", parent_session_id=None)

    def test_a_whole_session_over_the_pipe(self):
        self.assertEqual(self.ready["kind"], "ready")
        self.assertEqual(self.ready["pid"], self.proc.pid)
        self.start()
        first = self.until("awaiting_input")
        self.assertEqual([e["type"] for e in first], ["session_started", "model_called", "agent_turn",
                                                       "awaiting_input"])
        seqs = [e["seq"] for e in first]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(first[0]["workspace"], str(self.folder.resolve()))
        self.assertEqual(first[2]["text"], "reply 1: what is this?")

        self.send(kind="human_message", session_id="s1", task_id=None, content="and the tests?")
        second = self.until("awaiting_input")
        self.assertIn("reply 2: [Message from human]: and the tests?", [e.get("text") for e in second])

        self.send(kind="end_session", session_id="s1", task_id=None)
        ended = self.until("session_ended")[-1]
        self.assertEqual((ended["status"], ended["final_text"]), ("done", "reply 2: [Message from human]: and the tests?"))
        self.assertEqual(self.read(), {"kind": "exiting", "code": 0})
        self.assertEqual(self.proc.wait(10), 0)

        with Store.open(self.db) as store:
            stored = [e["type"] for e in store.events("s1")]
            self.assertEqual(store.session("s1")["status"], "done")
        self.assertEqual(stored[-1], "session_ended")
        self.assertIn("history_turn", stored)
        self.assertNotIn("history_turn", [e["type"] for e in first + second])

    def test_a_killed_worker_is_resumed_by_a_new_one(self):
        self.start()
        self.until("awaiting_input")
        self.proc.kill()
        self.proc.wait(10)
        self.proc.stdout.close()
        self.proc.stdin.close()
        env = {**os.environ, "SCRIPT_SHOWS_HISTORY": "1"}
        self.proc = subprocess.Popen([sys.executable, str(SCRIPTED), "--workspace", str(self.folder), "--db",
                                      str(self.db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.log, cwd=self.folder, env=env)
        self.read()
        self.send(kind="start_session", session_id="s1", resume=True, reason="worker gone")
        waiting = self.until("awaiting_input")
        self.assertEqual([e["type"] for e in waiting], ["session_resumed", "awaiting_input"])
        self.send(kind="human_message", session_id="s1", task_id=None, content="and now?")
        reply = [e for e in self.until("awaiting_input") if e["type"] == "agent_turn"][-1]
        self.assertEqual(reply["text"], "saw 4 turns, last: [Message from human]: and now?")
        with Store.open(self.db) as store:
            self.assertEqual(store.session("s1")["status"], "running")
            texts = [t["payload"]["turn"].get("text", "") for t in store.history_events("s1")
                     if t["type"] == "history_turn"]
        self.assertEqual(texts[:2], ["what is this?", "reply 1: what is this?"])
        self.assertTrue(texts[2].startswith("[Fox resumed this chat]"))

    def test_resuming_a_chat_with_no_history_says_so(self):
        with Store.open(self.db) as store:
            store.append_event("old", "session_started", {
                "task": "t", "workspace": str(self.folder), "mode": "agent", "permission_mode": "workspace-write",
                "model_key": None, "parent_id": None})
        self.send(kind="start_session", session_id="old", resume=True)
        ended = self.until("session_ended")[-1]
        self.assertEqual(ended["status"], "failed")
        self.assertIn("no stored history", ended["final_text"])

    def test_prints_from_agent_code_never_reach_the_protocol(self):
        self.start()
        self.until("awaiting_input")
        self.send(kind="shutdown")
        self.until("session_ended")
        self.proc.wait(10)
        for line in self.lines:
            json.loads(line)
        self.log.flush()
        log = (self.folder / "worker.log").read_text(encoding="utf-8")
        self.assertIn("noise from agent code", log)
        self.assertIn("raw noise on file descriptor 1", log)

    def test_stdin_closing_ends_every_session_as_interrupted_and_exits(self):
        self.start()
        self.until("awaiting_input")
        self.proc.stdin.close()
        ended = self.until("session_ended")[-1]
        self.assertEqual((ended["status"], ended["final_text"]), ("interrupted", "interrupted: server stopped"))
        self.assertEqual(self.proc.wait(10), 0)

    def test_unknown_sessions_and_bad_lines_get_errors_not_crashes(self):
        self.proc.stdin.write(b"this is not json\n")
        self.proc.stdin.flush()
        self.assertEqual(self.read()["kind"], "error")
        self.send(kind="human_message", session_id="nope", task_id=None, content="hi")
        error = self.read()
        self.assertEqual((error["kind"], error["session_id"]), ("error", "nope"))
        self.start()
        self.assertEqual(self.until("session_started")[0]["type"], "session_started")

    def test_a_side_chat_runs_in_the_same_process_and_shares_memory(self):
        self.start()
        self.until("awaiting_input")
        self.send(kind="start_session", session_id="side", task="why?", model_key="nemotron-super", mode="agent",
                  permission_mode="workspace-write", parent_session_id="s1")
        side = self.until("session_started")[-1]
        self.assertEqual((side["session_id"], side["parent_id"]), ("side", "s1"))
        self.assertIn("Question: why?", side["task"])

    def test_a_private_chat_writes_to_its_own_space(self):
        self.start()
        started = self.until("session_started")[-1]
        self.assertEqual(started["memory_space"], "s1")

    def test_a_shared_chat_joins_the_project_space_and_sees_a_capped_index(self):
        space = project_space(self.folder.resolve())
        with Store.open(self.db) as store:
            store.append_event("early", "session_started", {
                "task": "t", "workspace": str(self.folder), "mode": "agent", "permission_mode": "workspace-write",
                "model_key": None, "parent_id": None, "memory_space": space})
            for i in range(25):
                store.append_note("early", space, "earl.", {"task_id": None, "agent": "a", "scope": [],
                                                             "content": f"n{i}"})
        self.send(kind="start_session", session_id="s1", task="what is this?", model_key="nemotron-super",
                  mode="agent", permission_mode="workspace-write", parent_session_id=None, shared_memory=True)
        events = self.until("awaiting_input")
        self.assertEqual(events[0]["memory_space"], space)
        self.assertEqual(events[0]["task"], "what is this?")
        reply = next(e["text"] for e in events if e["type"] == "agent_turn")
        self.assertIn("written 25 entries before you joined", reply)
        self.assertIn("id=earl.note_25", reply)
        self.assertNotIn("id=earl.note_5 ", reply)

    def test_ready_is_fast(self):
        self.assertLess(self.ready_seconds, 3.0)


if __name__ == "__main__":
    unittest.main()
