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
from orchestration.claims import FileClaims
from orchestration.dispatch import TaskInbox
from orchestration.tasks import Lane, Task, TaskStatus

GOOD = {"outcome": "ok", "changes": [], "validation": "none", "blockers": []}


class FakeAgent:
    def __init__(self, behaviour="report", delay=0.0):
        self.behaviour = behaviour
        self.delay = delay
        self.gate = None
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
            if self.gate is not None:
                self.gate.wait(5)
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
        self.claims = FileClaims()
        self.inbox = TaskInbox()
        self.agent = FakeAgent()
        self.patch(dispatch, "run_agent", self.agent)
        self.patch(dispatch.routing, "pick_executor", lambda preferred=None: "fake-model")

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, task, mode=permissions.WORKSPACE_WRITE):
        return dispatch.start_task(task, mode, claims=self.claims, inbox=self.inbox, mem=self.mem,
                                   event_sink=lambda e, p: self.events.append((e, p)))

    def collect(self, n):
        finished = []
        while len(finished) < n:
            got = self.inbox.wait(5)
            self.assertTrue(got, "a task never reached the inbox")
            finished += got
        return finished

    def run_all(self, *lanes, mode=permissions.WORKSPACE_WRITE, scopes=None):
        tasks = [Task(f"t{i}", f"task {i}", lane, (scopes or {}).get(i, [])) for i, lane in enumerate(lanes, 1)]
        for t in tasks:
            self.start(t, mode)
        self.collect(len(tasks))
        return tasks

    def test_start_returns_before_the_task_finishes(self):
        self.agent.gate = threading.Event()
        task = Task("t1", "slow", Lane.READ, [])
        self.assertEqual(self.start(task), [])
        self.assertFalse(task.is_terminal)
        self.assertEqual(self.inbox.drain(), [])
        self.agent.gate.set()
        [finished] = self.collect(1)
        self.assertIs(finished.task.status, TaskStatus.DONE)

    def test_readers_overlap(self):
        self.agent.delay = 0.2
        tasks = self.run_all(Lane.READ, Lane.READ, Lane.READ)
        self.assertEqual(self.agent.peak[Lane.READ], 3)
        self.assertTrue(all(t.status is TaskStatus.DONE for t in tasks))

    def test_writers_on_different_files_overlap(self):
        self.agent.delay = 0.2
        tasks = self.run_all(Lane.WRITE, Lane.WRITE, scopes={1: ["a.py"], 2: ["b.py"]})
        self.assertEqual(self.agent.peak[Lane.WRITE], 2)
        self.assertTrue(all(t.status is TaskStatus.DONE for t in tasks))

    def test_writer_on_a_claimed_file_is_not_started(self):
        self.agent.gate = threading.Event()
        first, second = Task("t1", "a", Lane.WRITE, ["a.py"]), Task("t2", "b", Lane.WRITE, ["b.py", "a.py"])
        self.assertEqual(self.start(first), [])
        self.assertEqual(self.start(second), ["a.py"])
        self.assertIs(second.status, TaskStatus.FAILED)
        self.assertIn("a.py", second.error)
        self.assertIsNone(self.claims.owner("b.py"))
        self.agent.gate.set()
        self.collect(1)
        self.assertEqual([c["task_id"] for c in self.agent.calls], ["t1"])

    def test_claims_are_held_while_running_and_released_after(self):
        self.agent.gate = threading.Event()
        self.start(Task("t1", "a", Lane.WRITE, ["a.py"]))
        self.assertEqual(self.claims.owner("a.py"), "t1")
        self.agent.gate.set()
        self.collect(1)
        self.assertIsNone(self.claims.owner("a.py"))

    def test_readers_claim_nothing(self):
        self.agent.gate = threading.Event()
        self.start(Task("t1", "look", Lane.READ, ["a.py"]))
        self.assertIsNone(self.claims.owner("a.py"))
        self.agent.gate.set()
        self.collect(1)

    def test_executor_gets_the_claims(self):
        self.run_all(Lane.WRITE)
        self.assertIs(self.agent.calls[0]["claims"], self.claims)

    def test_report_means_done(self):
        [t] = self.run_all(Lane.WRITE)
        self.assertIs(t.status, TaskStatus.DONE)
        self.assertEqual(t.report.outcome, "ok")

    def test_no_report_means_failed(self):
        self.agent.behaviour = "no-report"
        [t] = self.run_all(Lane.WRITE)
        self.assertIs(t.status, TaskStatus.FAILED)
        self.assertIn("without calling report", t.error)

    def test_crash_means_failed_and_releases_the_claims(self):
        self.agent.behaviour = "crash"
        tasks = self.run_all(Lane.WRITE, Lane.WRITE, scopes={1: ["a.py"], 2: ["b.py"]})
        for t in tasks:
            self.assertIs(t.status, TaskStatus.FAILED)
            self.assertIn("TimeoutError", t.error)
        self.assertIsNone(self.claims.owner("a.py"))

    def test_policy_refusal_fails_before_running(self):
        [t] = self.run_all(Lane.WRITE, mode=permissions.READ_ONLY)
        self.assertIs(t.status, TaskStatus.FAILED)
        self.assertIn("read-only", t.error)
        self.assertEqual(self.agent.calls, [])

    def test_missing_model_fails_before_running(self):
        def no_model(preferred=None):
            raise routing.RoutingError("no key for kimi-k2")
        self.patch(dispatch.routing, "pick_executor", no_model)
        [t] = self.run_all(Lane.READ)
        self.assertIs(t.status, TaskStatus.FAILED)
        self.assertIn("kimi-k2", t.error)
        self.assertEqual(self.agent.calls, [])

    def test_reader_runs_read_only_without_write_tools(self):
        self.run_all(Lane.READ)
        [call] = self.agent.calls
        names = {t["name"] for t in call["tools"]}
        self.assertEqual(call["mode"], permissions.READ_ONLY)
        self.assertNotIn("write_file", names)
        self.assertIn("report", names)

    def test_executor_is_told_to_report(self):
        self.run_all(Lane.WRITE)
        self.assertIn("call the `report` tool", self.agent.calls[0]["text"])

    def test_every_task_is_logged_to_memory(self):
        self.run_all(Lane.READ, Lane.WRITE, mode=permissions.READ_ONLY)
        index = self.mem.read_index()
        self.assertIn("id=t1 agent=fake-model", index)
        self.assertIn("id=t2 agent=none", index)

    def test_events_announce_the_task_then_its_ending(self):
        self.run_all(Lane.READ)
        kinds = [e for e, _ in self.events]
        self.assertEqual(kinds[0], "task_created")
        self.assertEqual(self.events[0][1]["lane"], "read")
        statuses = [p["status"] for e, p in self.events if e == "task_status_changed"]
        self.assertEqual(statuses, ["running", "done"])
        self.assertEqual(kinds[-1], "memory_entry_added")

    def test_every_executed_task_is_scope_audited(self):
        self.agent.behaviour = "crash"
        self.run_all(Lane.READ, Lane.WRITE)
        self.start(Task("t3", "refused", Lane.WRITE, []), permissions.READ_ONLY)
        self.collect(1)
        audited = sorted(p["task_id"] for e, p in self.events if e == "scope_audit")
        self.assertEqual(audited, ["t1", "t2"])

    def test_audit_sees_the_executors_writes(self):
        def writes_undeclared(text, **kw):
            sink = kw["event_sink"]
            sink("tool_call", {"task_id": kw["task_id"], "name": "write_file", "args": {"path": "surprise.py"}})
            sink("tool_result", {"task_id": kw["task_id"], "name": "write_file",
                                 "content": '{"success": true}'})
            kw["extra_handlers"]["report"](GOOD)
        self.patch(dispatch, "run_agent", writes_undeclared)
        self.run_all(Lane.WRITE)
        [audit] = [p for e, p in self.events if e == "scope_audit"]
        self.assertEqual(audit["undeclared"], ["surprise.py"])
        self.assertTrue(audit["mispredicted"])
        self.assertIn("tool_call", [e for e, _ in self.events])

    def test_each_finished_task_carries_only_its_own_proposals(self):
        def proposes(text, **kw):
            kw["extra_handlers"]["ask_orchestrator"]({"proposal": f"idea from {kw['task_id']}"})
            kw["extra_handlers"]["report"](GOOD)
        self.patch(dispatch, "run_agent", proposes)
        self.start(Task("t1", "a", Lane.READ, []))
        self.start(Task("t2", "b", Lane.WRITE, []))
        finished = self.collect(2)
        self.assertEqual(sorted((f.task.id, f.proposals) for f in finished),
                         [("t1", ("idea from t1",)), ("t2", ("idea from t2",))])
        announced = sorted(p["task_id"] for e, p in self.events if e == "proposal_made")
        self.assertEqual(announced, ["t1", "t2"])

    def test_a_task_without_proposals_carries_none(self):
        self.start(Task("t1", "a", Lane.READ, []))
        [finished] = self.collect(1)
        self.assertEqual(finished.proposals, ())

    def test_sweep_catches_a_thread_that_died(self):
        def bug(task, session_mode):
            raise KeyError("a bug nobody expected")
        self.patch(dispatch, "admit", bug)
        self.patch(threading, "excepthook", lambda args: None)
        [t] = self.run_all(Lane.WRITE, scopes={1: ["a.py"]})
        self.assertIs(t.status, TaskStatus.FAILED)
        self.assertIn("without a final state", t.error)
        self.assertIsNone(self.claims.owner("a.py"))


class Inbox(unittest.TestCase):
    def test_wait_times_out_empty(self):
        self.assertEqual(TaskInbox().wait(0.05), [])

    def test_wait_wakes_when_a_task_is_put(self):
        inbox = TaskInbox()
        item = dispatch.Finished(Task("t1", "a", Lane.READ, []))
        threading.Timer(0.05, inbox.put, args=(item,)).start()
        self.assertEqual(inbox.wait(5), [item])
        self.assertEqual(inbox.drain(), [])


if __name__ == "__main__":
    unittest.main()
