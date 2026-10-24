import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from orchestration.tasks import (
    TRANSITIONS, InvalidTransition, Lane, Report, Task, TaskRegistry, TaskStatus, UnknownTask,
)

S = TaskStatus
REPORT = Report(outcome="added the flag", changes=["cli.py"], validation="ran --help", blockers=[])


def task_at(status: TaskStatus) -> Task:
    return Task(id="t1", description="d", lane=Lane.WRITE, scope=[], status=status)


class TransitionTable(unittest.TestCase):
    def test_every_status_has_a_row(self):
        self.assertEqual(set(TRANSITIONS), set(TaskStatus))

    def test_terminal_states(self):
        for s in (S.DONE, S.FAILED, S.CANCELLED, S.ESCALATED):
            self.assertTrue(task_at(s).is_terminal, s)
        for s in (S.QUEUED, S.RUNNING, S.NEEDS_APPROVAL):
            self.assertFalse(task_at(s).is_terminal, s)


class Transitions(unittest.TestCase):
    def test_happy_path(self):
        t = task_at(S.QUEUED)
        t.transition(S.RUNNING)
        t.transition(S.NEEDS_APPROVAL)
        t.transition(S.RUNNING)
        t.transition(S.DONE, report=REPORT)
        self.assertIs(t.status, S.DONE)
        self.assertIs(t.report, REPORT)

    def test_every_illegal_move_is_rejected(self):
        for src in TaskStatus:
            for dst in TaskStatus:
                if dst in TRANSITIONS[src]:
                    continue
                with self.subTest(src=src, dst=dst):
                    t = task_at(src)
                    with self.assertRaises(InvalidTransition):
                        t.transition(dst, report=REPORT, error="x")
                    self.assertIs(t.status, src)

    def test_cannot_skip_running(self):
        with self.assertRaises(InvalidTransition):
            task_at(S.QUEUED).transition(S.DONE, report=REPORT)

    def test_terminal_never_moves(self):
        t = task_at(S.DONE)
        with self.assertRaises(InvalidTransition):
            t.transition(S.RUNNING)

    def test_queued_can_be_cancelled_before_it_starts(self):
        t = task_at(S.QUEUED)
        t.transition(S.CANCELLED, error="batch cancelled")
        self.assertEqual(t.error, "batch cancelled")


class RequiredPayloads(unittest.TestCase):
    def test_done_requires_a_report(self):
        t = task_at(S.RUNNING)
        with self.assertRaises(InvalidTransition):
            t.transition(S.DONE)
        self.assertIs(t.status, S.RUNNING)
        self.assertIsNone(t.report)

    def test_report_and_error_are_set_before_the_status(self):
        seen = []

        class Watched(Task):
            def __setattr__(self, name, value):
                if name == "status":
                    seen.append((value, getattr(self, "report", None), getattr(self, "error", None)))
                super().__setattr__(name, value)

        done = Watched(id="t1", description="d", lane=Lane.WRITE, scope=[], status=S.RUNNING)
        done.transition(S.DONE, report=REPORT)
        failed = Watched(id="t2", description="d", lane=Lane.WRITE, scope=[], status=S.RUNNING)
        failed.transition(S.FAILED, error="boom")
        self.assertEqual(seen[1], (S.DONE, REPORT, None))
        self.assertEqual(seen[3], (S.FAILED, None, "boom"))

    def test_bad_endings_require_an_error(self):
        for s in (S.FAILED, S.CANCELLED, S.ESCALATED):
            with self.subTest(s=s):
                t = task_at(S.RUNNING)
                for empty in (None, ""):
                    with self.assertRaises(InvalidTransition):
                        t.transition(s, error=empty)
                self.assertIs(t.status, S.RUNNING)
                t.transition(s, error="ran out of turns")
                self.assertEqual(t.error, "ran out of turns")


class Registry(unittest.TestCase):
    def test_ids_are_sequential_and_start_queued(self):
        reg = TaskRegistry()
        a = reg.create("read auth", Lane.READ, [])
        b = reg.create("add flag", Lane.WRITE, ["cli.py"], model_key="gemini-flash")
        self.assertEqual((a.id, b.id), ("t1", "t2"))
        self.assertIs(a.status, S.QUEUED)
        self.assertEqual(b.model_key, "gemini-flash")

    def test_get_and_all(self):
        reg = TaskRegistry()
        a = reg.create("x", Lane.READ, [])
        b = reg.create("y", Lane.WRITE, [])
        self.assertIs(reg.get("t2"), b)
        self.assertEqual(reg.all(), [a, b])

    def test_unknown_id_raises(self):
        with self.assertRaises(UnknownTask):
            TaskRegistry().get("t99")

    def test_registries_are_independent(self):
        TaskRegistry().create("x", Lane.READ, [])
        self.assertEqual(TaskRegistry().create("y", Lane.READ, []).id, "t1")

    def test_scope_is_copied(self):
        scope = ["a.py"]
        t = TaskRegistry().create("x", Lane.WRITE, scope)
        scope.append("b.py")
        self.assertEqual(t.scope, ["a.py"])


if __name__ == "__main__":
    unittest.main()
