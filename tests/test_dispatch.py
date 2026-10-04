import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import memory
import permissions
from config import routing
from orchestration import dispatch
from orchestration.tasks import Lane, Task, TaskStatus

GOOD = {"outcome": "ok", "changes": [], "validation": "none", "blockers": []}


class FakeAgent:
    def __init__(self, behaviour="report", delay=0.0):
        self.behaviour = behaviour
        self.delay = delay
        self.calls = []
        self.lock = threading.Lock()
        self.active = {Lane.READ: 0, Lane.WRITE: 0}
        self.peak = {Lane.READ: 0, Lane.WRITE: 0}

    def __call__(self, text, **kw):
        lane = Lane.READ if kw["mode"] == permissions.READ_ONLY else Lane.WRITE
        with self.lock:
            self.calls.append({"text": text, **kw})
            self.active[lane] += 1
            self.peak[lane] = max(self.peak[lane], self.active[lane])
        try:
            time.sleep(self.delay)
            if self.behaviour == "crash":
                raise TimeoutError("provider timed out")
            if self.behaviour == "report":
                kw["extra_handlers"]["report"](GOOD)
            return "prose"
        finally:
            with self.lock:
                self.active[lane] -= 1


class DispatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mem = memory.Memory(Path(tmp.name) / "mem.md")
        self.events = []
        self.agent = FakeAgent()
        self.patch(dispatch, "run_agent", self.agent)
        self.patch(dispatch.routing, "pick_executor", lambda preferred=None: "fake-model")

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def batch(self, *lanes, mode=permissions.WORKSPACE_WRITE):
        tasks = [Task(f"t{i}", f"task {i}", lane, []) for i, lane in enumerate(lanes, 1)]
        dispatch.run_batch(tasks, mode, mem=self.mem,
                           event_sink=lambda e, p: self.events.append((e, p)))
        return tasks

    def test_readers_overlap(self):
        self.agent.delay = 0.2
        tasks = self.batch(Lane.READ, Lane.READ, Lane.READ)
        self.assertEqual(self.agent.peak[Lane.READ], 3)
        self.assertTrue(all(t.status is TaskStatus.DONE for t in tasks))

    def test_writers_never_overlap(self):
        self.agent.delay = 0.05
        tasks = self.batch(Lane.WRITE, Lane.WRITE, Lane.WRITE, Lane.WRITE)
        self.assertEqual(self.agent.peak[Lane.WRITE], 1)
        self.assertTrue(all(t.status is TaskStatus.DONE for t in tasks))

    def test_readers_are_not_blocked_by_a_writer(self):
        self.agent.delay = 0.2
        self.batch(Lane.WRITE, Lane.READ, Lane.READ)
        self.assertEqual(self.agent.peak[Lane.READ], 2)

    def test_run_batch_waits_for_slow_tasks(self):
        self.agent.delay = 0.3
        tasks = self.batch(Lane.READ, Lane.WRITE)
        self.assertEqual([t.status for t in tasks], [TaskStatus.DONE, TaskStatus.DONE])

    def test_report_means_done(self):
        [t] = self.batch(Lane.WRITE)
        self.assertIs(t.status, TaskStatus.DONE)
        self.assertEqual(t.report.outcome, "ok")

    def test_no_report_means_failed(self):
        self.agent.behaviour = "no-report"
        [t] = self.batch(Lane.WRITE)
        self.assertIs(t.status, TaskStatus.FAILED)
        self.assertIn("without calling report", t.error)

    def test_crash_means_failed_and_frees_the_slot(self):
        self.agent.behaviour = "crash"
        tasks = self.batch(Lane.WRITE, Lane.WRITE)
        for t in tasks:
            self.assertIs(t.status, TaskStatus.FAILED)
            self.assertIn("TimeoutError", t.error)

    def test_policy_refusal_fails_before_running(self):
        [t] = self.batch(Lane.WRITE, mode=permissions.READ_ONLY)
        self.assertIs(t.status, TaskStatus.FAILED)
        self.assertIn("read-only", t.error)
        self.assertEqual(self.agent.calls, [])

    def test_missing_model_fails_before_running(self):
        def no_model(preferred=None):
            raise routing.RoutingError("no key for kimi-k2")
        self.patch(dispatch.routing, "pick_executor", no_model)
        [t] = self.batch(Lane.READ)
        self.assertIs(t.status, TaskStatus.FAILED)
        self.assertIn("kimi-k2", t.error)
        self.assertEqual(self.agent.calls, [])

    def test_reader_runs_read_only_without_write_tools(self):
        self.batch(Lane.READ)
        [call] = self.agent.calls
        names = {t["name"] for t in call["tools"]}
        self.assertEqual(call["mode"], permissions.READ_ONLY)
        self.assertNotIn("write_file", names)
        self.assertIn("report", names)

    def test_executor_is_told_to_report(self):
        self.batch(Lane.WRITE)
        self.assertIn("call the `report` tool", self.agent.calls[0]["text"])

    def test_every_task_is_logged_to_memory(self):
        self.batch(Lane.READ, Lane.WRITE, mode=permissions.READ_ONLY)
        index = self.mem.read_index()
        self.assertIn("id=t1 agent=fake-model", index)
        self.assertIn("id=t2 agent=none", index)

    def test_events_announce_the_batch_then_each_ending(self):
        self.batch(Lane.READ)
        kinds = [e for e, _ in self.events]
        self.assertEqual(kinds[0], "task_created")
        self.assertEqual(self.events[0][1]["lane"], "read")
        statuses = [p["status"] for e, p in self.events if e == "task_status_changed"]
        self.assertEqual(statuses, ["running", "done"])
        self.assertEqual(kinds[-1], "memory_entry_added")

    def test_sweep_catches_a_thread_that_died(self):
        def bug(task, session_mode):
            raise KeyError("a bug nobody expected")
        self.patch(dispatch, "admit", bug)
        self.patch(threading, "excepthook", lambda args: None)
        [t] = self.batch(Lane.WRITE)
        self.assertIs(t.status, TaskStatus.FAILED)
        self.assertIn("without a final state", t.error)


if __name__ == "__main__":
    unittest.main()
