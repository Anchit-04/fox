import json
import os
import queue
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from sandbox import sandbox_client
from sandbox.sandbox_client import _Daemon


class FakeStdin:
    def __init__(self):
        self.requests: queue.Queue = queue.Queue()
        self.broken = False

    def write(self, text: str) -> None:
        if self.broken:
            raise BrokenPipeError("pipe closed")
        self.requests.put(json.loads(text))

    def flush(self) -> None:
        pass


class FakeStdout:
    def __init__(self):
        self.lines: queue.Queue = queue.Queue()

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class FakeProc:
    def __init__(self):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.killed = False

    def poll(self):
        return 1 if self.killed else None

    def kill(self) -> None:
        self.killed = True
        self.stdout.lines.put(None)

    def reply(self, request_id: str, output: str, exit_code: int = 0) -> None:
        self.stdout.lines.put(json.dumps({"id": request_id, "exit_code": exit_code, "output": output}) + "\n")


def call_in_background(daemon: _Daemon, command: str, timeout: float = 5.0) -> dict:
    box: dict = {}
    thread = threading.Thread(target=lambda: box.update(result=daemon.call({"command": command}, timeout)))
    thread.start()
    box["thread"] = thread
    return box


class DaemonRouting(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()
        self.daemon = _Daemon(self.proc)

    def tearDown(self):
        self.proc.kill()

    def test_replies_in_any_order_reach_the_right_caller(self):
        first = call_in_background(self.daemon, "echo first")
        second = call_in_background(self.daemon, "echo second")
        requests = [self.proc.stdin.requests.get(timeout=2) for _ in range(2)]
        by_command = {r["command"]: r["id"] for r in requests}

        self.proc.reply(by_command["echo second"], "second")
        self.proc.reply(by_command["echo first"], "first")
        first["thread"].join(2)
        second["thread"].join(2)

        self.assertEqual(first["result"], {"exit_code": 0, "output": "first"})
        self.assertEqual(second["result"], {"exit_code": 0, "output": "second"})

    def test_every_request_gets_its_own_id(self):
        for n in range(3):
            call_in_background(self.daemon, f"echo {n}")
        ids = {self.proc.stdin.requests.get(timeout=2)["id"] for _ in range(3)}
        self.assertEqual(len(ids), 3)

    def test_death_fails_every_waiting_caller_at_once(self):
        waiting = [call_in_background(self.daemon, f"echo {n}", timeout=30) for n in range(3)]
        for _ in range(3):
            self.proc.stdin.requests.get(timeout=2)

        started = time.monotonic()
        self.proc.kill()
        for box in waiting:
            box["thread"].join(2)

        self.assertLess(time.monotonic() - started, 2)
        for box in waiting:
            self.assertEqual(box["result"]["exit_code"], -1)
            self.assertIn("died unexpectedly", box["result"]["output"])
        self.assertFalse(self.daemon.alive())

    def test_malformed_reply_kills_the_daemon_and_fails_callers(self):
        waiting = call_in_background(self.daemon, "echo x")
        self.proc.stdin.requests.get(timeout=2)

        self.proc.stdout.lines.put("not json\n")
        waiting["thread"].join(2)

        self.assertIn("malformed response", waiting["result"]["output"])
        self.assertTrue(self.proc.killed)
        self.assertFalse(self.daemon.alive())

    def test_unknown_id_is_ignored(self):
        waiting = call_in_background(self.daemon, "echo x")
        request = self.proc.stdin.requests.get(timeout=2)

        self.proc.reply("someone-else", "stray")
        self.proc.reply(request["id"], "mine")
        waiting["thread"].join(2)

        self.assertEqual(waiting["result"]["output"], "mine")

    def test_timeout_kills_the_daemon(self):
        result = self.daemon.call({"command": "echo x"}, timeout=0.2)

        self.assertEqual(result["exit_code"], -1)
        self.assertIn("did not respond", result["output"])
        self.assertTrue(self.proc.killed)
        self.assertFalse(self.daemon.alive())

    def test_broken_pipe_is_reported(self):
        self.proc.stdin.broken = True

        result = self.daemon.call({"command": "echo x"}, timeout=1)

        self.assertEqual(result["exit_code"], -1)
        self.assertIn("pipe error", result["output"])
        self.assertEqual(self.daemon.pending, {})

    def test_a_dead_daemon_answers_without_writing(self):
        self.proc.kill()
        time.sleep(0.1)

        result = self.daemon.call({"command": "echo x"}, timeout=1)

        self.assertIn("died unexpectedly", result["output"])
        self.assertTrue(self.proc.stdin.requests.empty())


class OutputEncoding(unittest.TestCase):
    def start_env(self) -> dict:
        with mock.patch.object(sandbox_client.subprocess, "Popen") as popen, \
                mock.patch.object(sandbox_client._SANDBOXD_PATH.__class__, "exists", return_value=True):
            sandbox_client._start()
        return popen.call_args.kwargs["env"]

    def test_commands_write_utf8_through_the_pipe(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYTHONIOENCODING", None)
            self.assertEqual(self.start_env()["PYTHONIOENCODING"], "utf-8")

    def test_a_users_own_setting_wins(self):
        with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "cp1252"}):
            self.assertEqual(self.start_env()["PYTHONIOENCODING"], "cp1252")


@unittest.skipUnless(sandbox_client._SANDBOXD_PATH.exists(), "sandboxd.exe is not built")
class RealSandboxd(unittest.TestCase):
    @unittest.skipUnless(shutil.which("py"), "the py launcher is not installed")
    def test_python_can_print_an_emoji(self):
        result = json.loads(sandbox_client.run_bash_command('py -c "print(\'\\U0001f680 ok\')"'))
        self.assertEqual(result, {"exit_code": 0, "output": "\U0001f680 ok\r\n"})

    def test_parallel_commands_run_side_by_side(self):
        results: dict = {}

        def run(tag: str) -> None:
            results[tag] = json.loads(sandbox_client.run_bash_command(f"ping -n 3 127.0.0.1 >nul & echo {tag}"))

        sandbox_client.run_bash_command("echo warmup")
        threads = [threading.Thread(target=run, args=(tag,)) for tag in ("a", "b", "c", "d")]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertLess(time.monotonic() - started, 5)
        for tag in ("a", "b", "c", "d"):
            self.assertEqual(results[tag], {"exit_code": 0, "output": f"{tag}\r\n"})


if __name__ == "__main__":
    unittest.main()
