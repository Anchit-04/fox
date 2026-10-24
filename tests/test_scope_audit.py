import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import paths
from orchestration.scope_audit import ScopeAudit, covers, normalize
from orchestration.tasks import Lane, Task

OK = json.dumps({"success": True, "path": "x"})
ERR = json.dumps({"error": "old_str not found"})


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        patcher = mock.patch.object(paths, "_workspace", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)


class Normalize(WorkspaceCase):
    def test_spellings_of_one_file_agree(self):
        for spelling in ("cli.py", "./cli.py", "src/../cli.py", str(self.root / "cli.py")):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize(spelling), "cli.py")

    def test_forward_slashes_on_every_os(self):
        self.assertEqual(normalize("loom\\agent.py" if sys.platform == "win32" else "loom/agent.py"),
                         "loom/agent.py")

    def test_workspace_root_is_dot(self):
        self.assertEqual(normalize("."), ".")
        self.assertEqual(normalize("loom/"), "loom")

    def test_outside_the_workspace_stays_absolute(self):
        out = normalize("../elsewhere.py")
        self.assertTrue(Path(out).is_absolute())
        self.assertTrue(out.endswith("/elsewhere.py"))


class Covers(unittest.TestCase):
    def test_exact_file(self):
        self.assertTrue(covers("cli.py", "cli.py"))

    def test_folder_covers_its_contents(self):
        self.assertTrue(covers("loom", "loom/agent.py"))
        self.assertTrue(covers("loom", "loom/tools/file_tools.py"))

    def test_shared_prefix_is_not_a_folder(self):
        self.assertFalse(covers("loom", "loom2/x.py"))
        self.assertFalse(covers("cli", "cli.py"))

    def test_dot_covers_everything(self):
        self.assertTrue(covers(".", "anything/at/all.py"))


class AuditCase(WorkspaceCase):
    def make(self, scope, task_id="t1"):
        self.seen = []
        task = Task(task_id, "d", Lane.WRITE, scope)
        return ScopeAudit(task, lambda e, p: self.seen.append((e, p)))

    def write(self, audit, path, result, name="write_file", task_id="t1"):
        audit.sink("tool_call", {"task_id": task_id, "name": name, "args": {"path": path}})
        audit.sink("tool_result", {"task_id": task_id, "name": name, "content": result})


class Recording(AuditCase):
    def test_successful_writes_are_recorded(self):
        a = self.make(["cli.py"])
        self.write(a, "cli.py", OK)
        self.write(a, "./server.py", OK, name="edit_file")
        self.assertEqual(a.written, {"cli.py", "server.py"})

    def test_failed_write_is_not_recorded(self):
        a = self.make([])
        self.write(a, "cli.py", ERR)
        self.assertEqual(a.written, set())

    def test_denied_write_is_not_recorded(self):
        a = self.make([])
        a.sink("tool_call", {"task_id": "t1", "name": "write_file", "args": {"path": "cli.py"}})
        a.sink("tool_denied", {"task_id": "t1", "name": "write_file", "reason": "read-only"})
        a.sink("tool_result", {"task_id": "t1", "name": "write_file", "content": json.dumps({"error": "Refused"})})
        self.assertEqual(a.written, set())

    def test_reads_and_other_tasks_are_ignored(self):
        a = self.make([])
        self.write(a, "cli.py", OK, name="read_file")
        self.write(a, "cli.py", OK, task_id="t2")
        self.assertEqual(a.written, set())

    def test_garbage_results_and_paths_are_not_writes(self):
        a = self.make([])
        for result in ("not json", "[1, 2]", None, json.dumps({"success": "yes"})):
            self.write(a, "cli.py", result)
        for path in (None, "", 7):
            self.write(a, path, OK)
        self.assertEqual(a.written, set())

    def test_every_event_passes_through_unchanged(self):
        a = self.make([])
        events = [("agent_turn", {"task_id": "t1", "text": "hi"}),
                  ("tool_call", {"task_id": "t1", "name": "write_file", "args": {"path": "a"}}),
                  ("tool_result", {"task_id": "t1", "name": "write_file", "content": OK}),
                  ("tool_call", {"task_id": "t9", "name": "read_file", "args": {}})]
        for e, p in events:
            a.sink(e, p)
        self.assertEqual(self.seen, events)

    def test_no_inner_sink_is_fine(self):
        a = ScopeAudit(Task("t1", "d", Lane.WRITE, []), None)
        self.write(a, "cli.py", OK)
        a.finish()
        self.assertEqual(a.written, {"cli.py"})


class Verdict(AuditCase):
    def test_both_directions_of_misprediction(self):
        a = self.make(["cli.py", "docs"])
        self.write(a, "cli.py", OK)
        self.write(a, "server.py", OK)
        self.assertEqual(a.undeclared, {"server.py"})
        self.assertEqual(a.unused, {"docs"})

    def test_folder_scope_is_honoured(self):
        a = self.make(["loom/"])
        self.write(a, "loom/agent.py", OK)
        self.assertEqual((a.undeclared, a.unused), (set(), set()))

    def test_finish_emits_one_audit_event(self):
        a = self.make(["cli.py"])
        self.write(a, "server.py", OK)
        a.finish()
        name, payload = self.seen[-1]
        self.assertEqual(name, "scope_audit")
        self.assertEqual(payload, {
            "task_id": "t1", "lane": "write",
            "declared": ["cli.py"], "written": ["server.py"],
            "undeclared": ["server.py"], "unused": ["cli.py"],
            "mispredicted": True,
        })

    def test_accurate_scope_is_not_mispredicted(self):
        a = self.make(["cli.py"])
        self.write(a, "cli.py", OK)
        a.finish()
        self.assertFalse(self.seen[-1][1]["mispredicted"])


if __name__ == "__main__":
    unittest.main()
