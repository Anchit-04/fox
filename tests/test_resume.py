import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import resume
from orchestration.tasks import TaskStatus
from providers import ToolCall, ToolResult, Turn
from store import Store
from store.history_log import Journal


class ResumeRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.store = Store.open(self.folder / "fox.db")
        self.addCleanup(self.store.close)
        self.store.append_event("s1", "session_started", {
            "task": "fix billing", "workspace": str(self.folder), "mode": "orchestrator",
            "permission_mode": "workspace-write", "model_key": "claude-haiku", "parent_id": None})
        self.journal = Journal(self.store, "s1")
        self.emitted = []

    def emit(self, event_type, payload):
        self.emitted.append((event_type, payload))
        self.store.append_event("s1", event_type, payload)

    def task(self, task_id, status, description="d"):
        self.store.append_event("s1", "task_created", {"task_id": task_id, "description": description,
                                                       "lane": "write", "scope": ["shop/billing.py"]})
        if status != "queued":
            self.store.append_event("s1", "task_status_changed", {"task_id": task_id, "status": "running"})
        if status in ("done", "failed"):
            self.store.append_event("s1", "task_status_changed", {
                "task_id": task_id, "status": status, "error": "boom" if status == "failed" else None,
                "report": {"outcome": "ok", "changes": [], "validation": "v", "blockers": []}
                if status == "done" else None})

    def test_unfinished_tasks_are_stopped_and_finished_ones_left_alone(self):
        self.task("t1", "done")
        self.task("t2", "running", "add tests for billing")
        self.task("t3", "queued")
        stopped = resume.stop_unfinished_tasks(self.store, "s1", self.emit)
        self.assertEqual([row["task_id"] for row in stopped], ["t2", "t3"])
        statuses = {row["task_id"]: row["status"] for row in self.store._query("SELECT task_id, status FROM tasks")}
        self.assertEqual(statuses, {"t1": "done", "t2": "failed", "t3": "failed"})

    def test_the_registry_comes_back_and_numbering_continues(self):
        self.task("t1", "done")
        self.task("t2", "failed")
        registry = resume.restore_registry(self.store, "s1")
        self.assertEqual([t.id for t in registry.all()], ["t1", "t2"])
        self.assertEqual(registry.get("t1").status, TaskStatus.DONE)
        self.assertEqual(registry.get("t1").report.outcome, "ok")
        self.assertEqual(registry.create("next", registry.get("t1").lane, []).id, "t3")

    def test_a_chat_waiting_for_you_is_told_apart_from_one_mid_run(self):
        self.assertTrue(resume.was_waiting([Turn(role="user", text="hi"), Turn(role="assistant", text="hello")]))
        self.assertFalse(resume.was_waiting([Turn(role="assistant", tool_calls=[ToolCall("a", "read_file", {})])]))
        self.assertFalse(resume.was_waiting([Turn(role="user", text="[Fox resumed this chat]")]))

    def test_the_last_browser_page_is_found_in_the_history(self):
        turns = [Turn(role="user", tool_results=[ToolResult("a", "browser_open",
                                                            "Opened x.\n\nPage: Cart\nURL: http://localhost:5173/cart\n")]),
                 Turn(role="user", tool_results=[ToolResult("b", "read_file", "URL: not-a-browser-result")])]
        self.assertEqual(resume.last_url(turns), "http://localhost:5173/cart")

    def test_the_note_lists_stopped_tasks_and_what_changed_on_disk(self):
        subprocess.run(["git", "init", "-q"], cwd=self.folder, check=True)
        (self.folder / "billing.py").write_text("x = 1\n", encoding="utf-8")
        note = resume.resume_note(stopped_at="18:02", reason="server restarted",
                                  stopped=[{"task_id": "t2", "description": "add tests for billing"}],
                                  workspace=self.folder, url="http://localhost:5173/cart")
        self.assertTrue(note.startswith("[Fox resumed this chat] It stopped at 18:02 (server restarted)."))
        self.assertIn('t2 "add tests for billing"', note)
        self.assertIn("?? billing.py", note)
        self.assertIn("Your browser page was http://localhost:5173/cart.", note)
        self.assertTrue(note.endswith("Check the current state before redoing anything."))

    def test_outside_git_the_note_just_skips_it(self):
        note = resume.resume_note(stopped_at="t", reason="r", stopped=[], workspace=self.folder / "missing",
                                  url=None)
        self.assertNotIn("git", note)

    def test_prepare_needs_a_history(self):
        self.assertIsNone(resume.prepare(self.store, "s1", self.journal, self.emit))
        self.journal.recorder(None).turn(Turn(role="user", text="fix billing"))
        self.task("t1", "running")
        resumed = resume.prepare(self.store, "s1", self.journal, self.emit)
        self.assertFalse(resumed.waiting)
        self.assertEqual([row["task_id"] for row in resumed.stopped], ["t1"])
        self.assertIn("interrupted", self.store._query("SELECT error FROM tasks")[0]["error"])


if __name__ == "__main__":
    unittest.main()
