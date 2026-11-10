import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import external
import permissions
from config import routing
from external import claude_code, codex, runner, stream
from orchestration import dispatch
from orchestration.claims import FileClaims
from orchestration.executor_tools import ProposalBox
from orchestration.tasks import Lane, Task, TaskStatus

RECORDINGS = Path(__file__).resolve().parent / "fixtures" / "external"
WW = permissions.WORKSPACE_WRITE
_no_roster = mock.patch.object(routing.rosters, "load", return_value=routing.rosters.Roster())


def setUpModule():
    _no_roster.start()


def tearDownModule():
    _no_roster.stop()


def replay(translator_class, recording: str):
    events = []
    translator = translator_class("t1", lambda kind, payload: events.append((kind, payload)))
    for line in (RECORDINGS / recording).read_text(encoding="utf-8").splitlines():
        if line.strip():
            translator.feed(line)
    return translator, events


def kinds(events, kind):
    return [payload for k, payload in events if k == kind]


class CodexRecording(unittest.TestCase):
    def setUp(self):
        self.translator, self.events = replay(codex.Translator, "codex_write_edit_shell.jsonl")

    def test_file_changes_become_fox_write_events(self):
        calls = [(p["name"], Path(p["args"]["path"]).name) for p in kinds(self.events, "tool_call")
                 if p["name"] in ("write_file", "edit_file")]
        self.assertEqual(calls, [("write_file", "fox_probe.txt"), ("edit_file", "fox_probe.txt")])
        self.assertEqual(json.loads(kinds(self.events, "tool_result")[0]["content"]), {"success": True})

    def test_a_command_keeps_its_output_and_exit_code(self):
        shell = [p for p in kinds(self.events, "tool_result") if p["name"] == "shell"][0]
        self.assertEqual(json.loads(shell["content"]), {"exit_code": 0, "output": "hello\r\n"})

    def test_the_last_message_is_the_outcome(self):
        outcome = self.translator.outcome(0, "")
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.final_text.startswith("Created fox_probe.txt"))
        self.assertEqual(outcome.session_id, "01a1c0b4-076e-7671-be50-0803c93f95a7")

    def test_tokens_are_reported_with_cache_reads_split_out(self):
        call = kinds(self.events, "model_called")[0]
        self.assertEqual((call["input_tokens"], call["cache_read_tokens"], call["output_tokens"]), (2735, 27392, 176))
        self.assertIsNone(call["cost"])
        self.assertEqual(set(call), {"task_id", "model_key", "duration_ms", "input_tokens", "output_tokens",
                                     "cache_read_tokens", "cache_write_tokens", "cost", "cost_source", "prefix"})

    def test_every_event_belongs_to_the_task(self):
        self.assertTrue(all(payload["task_id"] == "t1" for _, payload in self.events))


class CodexBlocked(unittest.TestCase):
    def test_a_run_that_changed_nothing_still_finishes(self):
        translator, events = replay(codex.Translator, "codex_blocked.jsonl")
        self.assertEqual(kinds(events, "tool_call"), [])
        outcome = translator.outcome(0, "ERROR ... rejected: blocked by policy")
        self.assertTrue(outcome.ok)
        self.assertIn("blocked", outcome.final_text)


class ClaudeRealRun(unittest.TestCase):
    def setUp(self):
        self.translator, self.events = replay(claude_code.Translator, "claude_write_edit_bash.jsonl")

    def test_write_and_edit_become_fox_write_events(self):
        writes = [(p["name"], Path(p["args"]["path"]).name) for p in kinds(self.events, "tool_call")
                  if p["name"] in ("write_file", "edit_file")]
        self.assertEqual(writes, [("write_file", "fox_probe.txt"), ("edit_file", "fox_probe.txt")])

    def test_its_shell_tool_keeps_its_name_and_output(self):
        shell = [p for p in kinds(self.events, "tool_result") if p["name"] == "PowerShell"]
        self.assertEqual(shell[0]["content"], "hello")

    def test_the_summary_cost_and_session_come_through(self):
        outcome = self.translator.outcome(0, "")
        self.assertTrue(outcome.ok)
        self.assertIn("Files changed", outcome.final_text)
        self.assertEqual(outcome.session_id, "2403147b-bdd9-4ea0-ade1-48343b30d956")
        call = kinds(self.events, "model_called")
        self.assertEqual(len(call), 1)
        self.assertEqual((call[0]["cost"], call[0]["cost_source"]), (0.142082, "provider"))

    def test_lines_it_does_not_use_are_ignored_quietly(self):
        self.assertEqual(self.translator.noise, [])
        self.assertEqual({k for k, _ in self.events}, {"tool_call", "tool_result", "agent_turn", "model_called"})


class ClaudeRecording(unittest.TestCase):
    def test_an_expired_login_is_a_failure_with_its_reason(self):
        translator, events = replay(claude_code.Translator, "claude_auth_expired.jsonl")
        outcome = translator.outcome(1, "")
        self.assertFalse(outcome.ok)
        self.assertIn("OAuth session expired", outcome.failure)
        self.assertEqual(outcome.session_id, "d6b8bea7-2dbc-43c6-a490-1fbe6a1c8fd6")


class ClaudeToolCalls(unittest.TestCase):
    def feed(self, *events):
        out = []
        translator = claude_code.Translator("t1", lambda kind, payload: out.append((kind, payload)))
        for event in events:
            translator.feed(json.dumps(event))
        return translator, out

    def use(self, call_id, name, args):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": call_id, "name": name,
                                                              "input": args}]}}

    def result(self, call_id, content, error=False):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call_id,
                                                         "content": content, "is_error": error}]}}

    def test_parallel_calls_are_paired_by_id(self):
        _, out = self.feed(self.use("a", "Write", {"file_path": "a.py"}), self.use("b", "Edit", {"file_path": "b.py"}),
                           self.result("b", "ok"), self.result("a", "ok"))
        pairs = [(k, p["name"], p.get("args", {}).get("path")) for k, p in out]
        self.assertEqual(pairs, [("tool_call", "edit_file", "b.py"), ("tool_result", "edit_file", None),
                                 ("tool_call", "write_file", "a.py"), ("tool_result", "write_file", None)])

    def test_a_failed_edit_is_not_a_success(self):
        _, out = self.feed(self.use("a", "Edit", {"file_path": "a.py"}), self.result("a", "old_string not found", True))
        self.assertEqual(json.loads(out[1][1]["content"]), {"success": False, "error": "old_string not found"})

    def test_other_tools_keep_their_own_name(self):
        _, out = self.feed(self.use("a", "Bash", {"command": "npm test"}),
                           self.result("a", [{"type": "text", "text": "3 passed"}]))
        self.assertEqual((out[0][1]["name"], out[0][1]["args"]), ("Bash", {"command": "npm test"}))
        self.assertEqual(out[1][1]["content"], "3 passed")

    def test_a_successful_result_is_the_outcome_and_the_cost(self):
        translator, out = self.feed({"type": "result", "subtype": "success", "is_error": False, "result": "Done.",
                                     "session_id": "s1", "total_cost_usd": 0.0123, "duration_ms": 900,
                                     "usage": {"input_tokens": 10, "output_tokens": 5,
                                               "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7}})
        outcome = translator.outcome(0, "")
        self.assertEqual((outcome.ok, outcome.final_text, outcome.session_id), (True, "Done.", "s1"))
        call = kinds(out, "model_called")[0]
        self.assertEqual((call["cost"], call["cost_source"], call["cache_write_tokens"]), (0.0123, "provider", 7))


class Outcomes(unittest.TestCase):
    def test_no_final_event_is_a_failure_with_stderr(self):
        translator = codex.Translator("t1", None)
        outcome = translator.outcome(2, "boom: not logged in")
        self.assertFalse(outcome.ok)
        self.assertIn("exit code 2", outcome.failure)
        self.assertIn("not logged in", outcome.failure)

    def test_lines_that_are_not_json_are_kept_for_the_error(self):
        translator = codex.Translator("t1", None)
        translator.feed("Error: something broke")
        self.assertIn("something broke", translator.outcome(1, "").failure)


class Commands(unittest.TestCase):
    def test_claude_code_modes(self):
        for mode, flag in ((permissions.READ_ONLY, "plan"), (WW, "acceptEdits"),
                           (permissions.FULL_ACCESS, "bypassPermissions")):
            args = claude_code.command("claude", mode, None)
            self.assertEqual(args[args.index("--permission-mode") + 1], flag)
        self.assertEqual(claude_code.command("claude", WW, "s1")[-2:], ["--resume", "s1"])

    def test_codex_modes_and_resume(self):
        with mock.patch.object(codex, "_windows_sandbox_chosen", return_value=True):
            fresh = codex.command("codex", permissions.READ_ONLY, None)
            again = codex.command("codex", WW, "th1")
        self.assertEqual(fresh, ["codex", "exec", "--json", "--skip-git-repo-check", "-c", "sandbox_mode=read-only", "-"])
        self.assertEqual(again[:3], ["codex", "exec", "resume"])
        self.assertEqual(again[-2:], ["th1", "-"])

    def test_codex_gets_a_windows_sandbox_only_when_none_is_configured(self):
        with mock.patch.object(codex.os, "name", "nt"), \
                mock.patch.object(codex, "_windows_sandbox_chosen", return_value=False):
            self.assertIn("windows.sandbox=unelevated", codex.command("codex", WW, None))
        with mock.patch.object(codex.os, "name", "nt"), \
                mock.patch.object(codex, "_windows_sandbox_chosen", return_value=True):
            self.assertNotIn("windows.sandbox=unelevated", codex.command("codex", WW, None))

    def test_the_shell_line_survives_cmds_quote_stripping(self):
        line = stream.shell_line([r"C:\a b\codex.cmd", "exec", "-c", "sandbox_mode=read-only", "-"],
                                 Path(r"C:\t\brief.md"), Path(r"C:\t\err.txt"))
        self.assertEqual(line, r'""C:\a b\codex.cmd" exec -c "sandbox_mode=read-only" - < "C:\t\brief.md" 2> "C:\t\err.txt""')


class Follow(unittest.TestCase):
    def run_follow(self, start, *reads):
        lines = []
        with mock.patch.object(stream.sandbox_client, "start", return_value=start), \
                mock.patch.object(stream.sandbox_client, "read", side_effect=list(reads)), \
                mock.patch.object(stream.time, "sleep"):
            code = stream.follow("x", lines.append)
        return code, lines

    def test_lines_split_across_reads_are_joined(self):
        code, lines = self.run_follow({"process": "p1", "status": "running", "output": '{"a":'},
                                      {"process": "p1", "status": "running", "output": ' 1}\n{"b"'},
                                      {"process": "p1", "status": "exited", "exit_code": 0, "output": ': 2}\n'})
        self.assertEqual((code, lines), (0, ['{"a": 1}', '{"b": 2}']))

    def test_a_quick_exit_needs_no_reads(self):
        code, lines = self.run_follow({"process": "p1", "status": "exited", "exit_code": 3, "output": "last"})
        self.assertEqual((code, lines), (3, ["last"]))

    def test_a_refused_start_raises(self):
        with self.assertRaisesRegex(stream.ExternalError, "the most allowed"):
            self.run_follow({"exit_code": -1, "output": "8 background processes ... the most allowed"})

    def test_losing_the_process_raises(self):
        with self.assertRaisesRegex(stream.ExternalError, "no background process"):
            self.run_follow({"process": "p1", "status": "running", "output": ""},
                            {"exit_code": -1, "output": "no background process \"p1\""})


class Routing(unittest.TestCase):
    def test_an_installed_external_agent_is_picked_by_name(self):
        with mock.patch.object(external, "available", return_value=True):
            self.assertEqual(routing.pick_executor("codex"), "codex")

    def test_a_missing_one_is_a_routing_error(self):
        with mock.patch.object(external, "available", return_value=False):
            with self.assertRaisesRegex(routing.RoutingError, "isn't installed"):
                routing.pick_executor("claude-code")

    def test_the_brief_names_the_scope_and_asks_for_a_summary(self):
        text = external.brief("Fix the bug.", ["shop/users.py"])
        self.assertIn("shop/users.py", text)
        self.assertIn("finish with a short summary", text)


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.sink = lambda kind, payload: self.events.append((kind, payload))
        self.calls = []

    def fake_run(self, outcome, writes=()):
        def run(name, text, mode, *, task_id, event_sink, resume=None):
            self.calls.append({"name": name, "text": text, "mode": mode, "resume": resume})
            for path in writes:
                event_sink("tool_call", {"task_id": task_id, "name": "edit_file", "args": {"path": path}})
                event_sink("tool_result", {"task_id": task_id, "name": "edit_file", "content": '{"success": true}'})
            return outcome
        return mock.patch.object(external, "run", side_effect=run)

    def start(self, task):
        inbox = dispatch.TaskInbox()
        mem = mock.Mock()
        with mock.patch.object(external, "available", return_value=True):
            dispatch.start_task(task, WW, claims=FileClaims(), inbox=inbox, mem=mem, event_sink=self.sink)
            return inbox.wait(10)

    def test_a_finished_run_is_a_done_task_with_the_files_it_wrote(self):
        task = Task(id="t1", description="Rename it.", lane=Lane.WRITE, scope=["shop/users.py"], model_key="codex")
        with self.fake_run(external.Outcome(True, "Renamed.", None, "th1"), writes=["shop/users.py"]):
            self.start(task)
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertEqual((task.report.outcome, task.report.changes), ("Renamed.", ["shop/users.py"]))
        self.assertEqual(task.external_session, "th1")
        self.assertEqual(self.calls[0]["mode"], WW)
        audit = kinds(self.events, "scope_audit")[0]
        self.assertFalse(audit["mispredicted"])

    def test_a_failed_run_is_a_failed_task_with_the_reason(self):
        task = Task(id="t1", description="x", lane=Lane.WRITE, scope=[], model_key="claude-code")
        with self.fake_run(external.Outcome(False, "", "Failed to authenticate", None)):
            self.start(task)
        self.assertEqual((task.status, task.error), (TaskStatus.FAILED, "Failed to authenticate"))

    def test_a_crash_is_a_failed_task_not_a_dead_thread(self):
        task = Task(id="t1", description="x", lane=Lane.READ, scope=[], model_key="codex")
        with mock.patch.object(external, "run", side_effect=stream.ExternalError("could not start")):
            self.start(task)
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIn("could not start", task.error)

    def test_a_read_task_runs_read_only(self):
        task = Task(id="t1", description="Look.", lane=Lane.READ, scope=[], model_key="codex")
        with self.fake_run(external.Outcome(True, "Seen.", None, "th1")):
            self.start(task)
        self.assertEqual(self.calls[0]["mode"], permissions.READ_ONLY)

    def test_a_follow_up_resumes_the_same_session(self):
        task = Task(id="t1", description="Rename it.", lane=Lane.WRITE, scope=[], model_key="codex")
        with self.fake_run(external.Outcome(True, "Renamed.", None, "th1")):
            self.start(task)
        inbox = dispatch.TaskInbox()
        journal = mock.Mock()
        journal.turns.return_value = []
        with self.fake_run(external.Outcome(True, "Also fixed the docstring.", None, "th1")), \
                mock.patch.object(external, "available", return_value=True):
            dispatch.continue_task(task, "Fix the docstring too.", WW, claims=FileClaims(), inbox=inbox,
                                   mem=mock.Mock(), journal=journal, event_sink=self.sink)
            inbox.wait(10)
        self.assertEqual(self.calls[-1]["resume"], "th1")
        self.assertEqual(self.calls[-1]["text"], "Fix the docstring too.")
        self.assertEqual((task.status, task.run), (TaskStatus.DONE, 2))


if __name__ == "__main__":
    unittest.main()
