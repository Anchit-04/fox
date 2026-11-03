import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loom"))

from store import Store

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
        self.assertEqual([e["seq"] for e in first], [1, 2, 3, 4])
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
        self.assertEqual((ended["status"], ended["final_text"]), ("failed", "interrupted: server stopped"))
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

    def test_ready_is_fast(self):
        self.assertLess(self.ready_seconds, 3.0)


if __name__ == "__main__":
    unittest.main()
