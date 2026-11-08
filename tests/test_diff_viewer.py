import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import baselines
import diffs
import paths
import worker
from agent import run_tool_call
from store import Store
from test_hub import HubCase


class Call:
    def __init__(self, name, args):
        self.name, self.args, self.raw = name, args, None


class Workspace(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        before = paths.workspace()
        paths.set_workspace(self.dir.name, allow_unsafe=True)
        self.addCleanup(paths.set_workspace, before, allow_unsafe=True)
        baselines._seen.clear()


class CapturingBaselines(Workspace):
    def run_call(self, sink, name, **args):
        return run_tool_call(Call(name, args), mode="workspace-write", approver=None, model_key="m",
                             todo_manager=None, mem=None, event_sink=sink)

    def test_the_first_edit_records_the_file_as_it_was(self):
        (self.root / "a.py").write_bytes(b"one\n")
        seen = []
        sink = lambda kind, payload: seen.append((kind, payload))
        self.run_call(sink, "edit_file", path="a.py", old_str="one", new_str="two")
        self.run_call(sink, "edit_file", path="a.py", old_str="two", new_str="three")
        found = [p for kind, p in seen if kind == baselines.EVENT]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["path"], found[0]["existed"], found[0]["content"]), ("a.py", True, "one\n"))

    def test_a_new_file_has_an_empty_baseline(self):
        seen = []
        self.run_call(lambda kind, payload: seen.append(payload), "write_file", path="new/b.py", content="x")
        self.assertEqual((seen[0]["path"], seen[0]["existed"], seen[0]["content"]), ("new/b.py", False, ""))

    def test_reads_and_other_tools_record_nothing(self):
        (self.root / "a.py").write_text("x", encoding="utf-8")
        seen = []
        self.run_call(lambda kind, payload: seen.append(kind), "read_file", path="a.py")
        self.assertNotIn(baselines.EVENT, seen)

    def test_binary_and_large_files_are_marked_not_text(self):
        (self.root / "a.bin").write_bytes(b"\0\1\2")
        self.assertEqual(baselines.read(self.root / "a.bin"), (True, None))
        with mock.patch.object(baselines, "MAX_BYTES", 2):
            (self.root / "big.txt").write_text("abc", encoding="utf-8")
            self.assertEqual(baselines.read(self.root / "big.txt"), (True, None))


class StoringBaselines(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store.open(Path(self.dir.name) / "fox.db")
        self.addCleanup(self.store.close)
        self.store.append_event("s1", "session_started", {"task": "t", "workspace": self.dir.name, "mode": "agent",
                                                          "permission_mode": "workspace-write", "model_key": "m",
                                                          "parent_id": None, "memory_space": None})

    def test_the_first_baseline_wins(self):
        self.store.put_baseline("s1", "a.py", True, "first")
        self.store.put_baseline("s1", "a.py", True, "second")
        self.assertEqual(self.store.baselines("s1"), [{"path": "a.py", "existed": 1, "content": "first"}])

    def test_deleting_the_chat_deletes_its_baselines(self):
        self.store.put_baseline("s1", "a.py", True, "x")
        self.store.delete_session("s1")
        self.assertEqual(self.store.baselines("s1"), [])

    def test_the_worker_stores_baselines_instead_of_logging_them(self):
        session = worker.Session(self.store, "s1")
        with mock.patch.object(worker, "send") as send:
            session.handle_event(baselines.EVENT, {"task_id": None, "path": "a.py", "existed": True, "content": "x"})
        send.assert_not_called()
        self.assertEqual(len(self.store.baselines("s1")), 1)
        self.assertFalse(self.store.events("s1", [baselines.EVENT]))


class FoxChanges(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def test_modified_added_deleted_and_unchanged(self):
        (self.root / "mod.py").write_bytes(b"a\nb\nc\n")
        (self.root / "new.py").write_bytes(b"hello\n")
        (self.root / "same.py").write_bytes(b"x\n")
        rows = [{"path": "mod.py", "existed": 1, "content": "a\nB\nc\n"},
                {"path": "new.py", "existed": 0, "content": ""},
                {"path": "gone.py", "existed": 1, "content": "bye\n"},
                {"path": "same.py", "existed": 1, "content": "x\n"},
                {"path": "never.py", "existed": 0, "content": ""}]
        files, note = diffs.fox_changes(rows, self.root)
        by = {f["path"]: f for f in files}
        self.assertEqual(sorted(by), ["gone.py", "mod.py", "new.py"])
        self.assertEqual((by["mod.py"]["status"], by["mod.py"]["added"], by["mod.py"]["removed"]), ("modified", 1, 1))
        self.assertIn("-B", by["mod.py"]["diff"])
        self.assertIn("+b", by["mod.py"]["diff"])
        self.assertEqual((by["new.py"]["status"], by["new.py"]["added"]), ("added", 1))
        self.assertIn("--- /dev/null", by["new.py"]["diff"])
        self.assertEqual((by["gone.py"]["status"], by["gone.py"]["removed"]), ("deleted", 1))
        self.assertEqual(note, "")

    def test_only_line_endings(self):
        (self.root / "a.py").write_bytes(b"x\r\ny\r\n")
        files, _ = diffs.fox_changes([{"path": "a.py", "existed": 1, "content": "x\ny\n"}], self.root)
        self.assertEqual((files[0]["note"], files[0]["added"]), ("only the line endings changed", 0))

    def test_nothing_changed_yet(self):
        files, note = diffs.fox_changes([], self.root)
        self.assertEqual(files, [])
        self.assertIn("hasn't changed any files", note)

    def test_binary_files_get_a_note(self):
        (self.root / "a.bin").write_bytes(b"\0x")
        files, _ = diffs.fox_changes([{"path": "a.bin", "existed": 1, "content": None}], self.root)
        self.assertEqual(files[0]["note"], diffs.NOT_SHOWN)

    def test_long_diffs_are_cut(self):
        (self.root / "a.txt").write_text("\n".join(map(str, range(50))), encoding="utf-8")
        with mock.patch.object(diffs, "MAX_LINES", 10):
            files, _ = diffs.fox_changes([{"path": "a.txt", "existed": 0, "content": ""}], self.root)
        self.assertEqual(len(files[0]["diff"].splitlines()), 10)
        self.assertEqual(files[0]["added"], 50)
        self.assertIn("cut at 10 lines", files[0]["note"])


class GitChanges(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.root,
                       capture_output=True, timeout=20, check=True)

    def test_not_a_repository(self):
        files, note = diffs.git_changes(self.root)
        self.assertEqual(files, [])
        self.assertIn("isn't a git repository", note)

    def test_tracked_edits_deletions_and_untracked_files(self):
        self.git("init", "-q")
        (self.root / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        (self.root / "b.py").write_text("bye\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "start")
        (self.root / "a.py").write_text("one\nTWO\n", encoding="utf-8")
        (self.root / "b.py").unlink()
        (self.root / "c.py").write_text("new\n", encoding="utf-8")
        files, note = diffs.git_changes(self.root)
        by = {f["path"]: f for f in files}
        self.assertEqual((by["a.py"]["status"], by["a.py"]["added"], by["a.py"]["removed"]), ("modified", 1, 1))
        self.assertEqual(by["b.py"]["status"], "deleted")
        self.assertEqual((by["c.py"]["status"], by["c.py"]["added"]), ("untracked", 1))
        self.assertTrue(by["a.py"]["diff"].startswith("--- a/a.py"))

    def test_a_clean_tree(self):
        self.git("init", "-q")
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "start")
        self.assertEqual(diffs.git_changes(self.root), ([], "No uncommitted changes."))

    def test_a_repository_with_no_commits_yet(self):
        self.git("init", "-q")
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        files, _ = diffs.git_changes(self.root)
        self.assertEqual([f["path"] for f in files], ["a.py"])


class TheHubAnswersFileDiff(HubCase):
    async def test_fox_and_git_sources(self):
        tui = await self.client()
        session_id = await tui.start()
        await tui.event(session_id, "awaiting_input")
        (self.project_a / "a.py").write_bytes(b"after\n")
        self.store.put_baseline(session_id, "a.py", True, "before\n")
        await tui.send(type="file_diff", session_id=session_id)
        reply = await tui.until(lambda m: m.get("type") == "file_diff")
        self.assertEqual(reply["source"], "fox")
        self.assertEqual([f["path"] for f in reply["files"]], ["a.py"])
        self.assertIn("+after", reply["files"][0]["diff"])
        await tui.send(type="file_diff", session_id=session_id, source="git")
        reply = await tui.until(lambda m: m.get("type") == "file_diff")
        self.assertEqual(reply["source"], "git")
        self.assertIn("isn't a git repository", reply["note"])

    async def test_unknown_session(self):
        tui = await self.client()
        await tui.send(type="file_diff", session_id="nope")
        reply = await tui.until(lambda m: m.get("type") == "error")
        self.assertIn("unknown session_id", reply["message"])


if __name__ == "__main__":
    unittest.main()
