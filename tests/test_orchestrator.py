import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
import context
import memory
import permissions
from injection import InjectionQueue
from orchestration import dispatch
from orchestration import orchestrator as orch
from providers import ProviderResponse, ToolCall


def delegate(call_id, description, lane="read", scope=None, **extra):
    return ToolCall(call_id, "delegate_task",
                    {"description": description, "lane": lane, "scope": scope or [], **extra})


def reply(*calls, text=None):
    return ProviderResponse(text=text, tool_calls=list(calls))


def done(text="All done."):
    return ProviderResponse(text=text, tool_calls=[])


def wait(call_id="w"):
    return reply(ToolCall(call_id, "wait_for_tasks", {}))


def seen(request) -> str:
    parts = []
    for turn in request["history"]:
        parts.append(turn.text or "")
        parts.extend(r.content for r in turn.tool_results)
    return "\n".join(parts)


class ScriptedModel:
    def __init__(self, *responses, forever=None, limit=100):
        self.responses = list(responses)
        self.forever = forever
        self.limit = limit
        self.requests = []

    def generate(self, history, tools, system_prompt):
        if len(self.requests) >= self.limit:
            raise RuntimeError(f"orchestrator called the model more than {self.limit} times")
        self.requests.append({"history": list(history), "tools": tools, "prompt": system_prompt})
        if self.responses:
            response = self.responses.pop(0)
            return response() if callable(response) else response
        return self.forever() if self.forever else done("(script exhausted)")

    def results_seen(self, n):
        [*_, last] = [t for t in self.requests[n]["history"] if t.tool_results]
        return [r.content for r in last.tool_results]


def executor(text, **kw):
    kw["extra_handlers"]["report"]({
        "outcome": f"finished: {text.splitlines()[0]}",
        "changes": ["cli.py"] if kw["mode"] != permissions.READ_ONLY else [],
        "validation": "none",
        "blockers": [],
    })


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mem = memory.Memory(Path(tmp.name) / "mem.md")
        self.events = []
        self.executor_calls = []

        def recording_executor(text, **kw):
            self.executor_calls.append(text)
            executor(text, **kw)

        self.patch(dispatch, "run_agent", recording_executor)
        self.patch(dispatch.routing, "pick_executor", lambda preferred=None: "fake-exec")

        self.bash_runs = []
        self.patch(agent, "run_bash_command", lambda command: self.bash_runs.append(command) or "ran")

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def file_handler(self, name, handler):
        patcher = mock.patch.dict(agent.FILE_TOOL_HANDLERS, {name: handler})
        patcher.start()
        self.addCleanup(patcher.stop)

    def task_status(self, task_id):
        statuses = [p["status"] for e, p in self.events if e == "task_status_changed" and p["task_id"] == task_id]
        return statuses[-1] if statuses else None

    def run_with(self, model, mode=permissions.WORKSPACE_WRITE, **kw):
        self.patch(orch, "get_provider", lambda key: model)
        kw.setdefault("model_key", "claude-opus")
        return orch.run_orchestrator("add a --verbose flag", mem=self.mem, mode=mode,
                                     event_sink=lambda e, p: self.events.append((e, p)), **kw)

    def test_dependency_flows_through_the_conversation(self):
        model = ScriptedModel(
            reply(delegate("a", "find the env loader")),
            wait("w1"),
            reply(delegate("b", "add --verbose using what t1 found", lane="write", scope=["cli.py"])),
            wait("w2"),
            done("Added --verbose."),
        )
        final = self.run_with(model)
        self.assertEqual(final, "Added --verbose.")
        self.assertIn("[task finished] t1 [read] done: finished: find the env loader", seen(model.requests[2]))
        self.assertIn("[task finished] t2 [write] done: finished: add --verbose using what t1 found | changed cli.py",
                      seen(model.requests[4]))

    def test_delegate_returns_at_once_with_the_task_id(self):
        model = ScriptedModel(reply(delegate("a", "one"), delegate("b", "two")), wait(), wait("w2"), done())
        self.run_with(model)
        first, second = model.results_seen(1)
        self.assertTrue(first.startswith("t1 started [read]"))
        self.assertTrue(second.startswith("t2 started [read]"))

    def test_orchestrator_keeps_working_while_a_task_runs(self):
        gate = threading.Event()
        statuses = []

        def slow(text, **kw):
            gate.wait(5)
            executor(text, **kw)

        def own_write(args):
            statuses.append(self.task_status("t1"))
            gate.set()
            return '{"success": true}'

        self.patch(dispatch, "run_agent", slow)
        self.file_handler("write_file", own_write)
        model = ScriptedModel(
            reply(delegate("a", "write the docs", lane="write", scope=["docs.md"])),
            reply(ToolCall("w", "write_file", {"path": "cli.py", "content": "x"})),
            wait(),
            done(),
        )
        self.run_with(model)
        self.assertEqual(statuses, ["running"])
        self.assertIn("[task finished] t1 [write] done", seen(model.requests[3]))

    def test_results_come_back_in_call_order(self):
        model = ScriptedModel(
            reply(delegate("a", "first"),
                  ToolCall("m", "memory_write", {"content": "note"}),
                  delegate("b", "second")),
            wait(), wait("w2"),
            done(),
        )
        self.run_with(model)
        [results] = [t.tool_results for t in model.requests[1]["history"] if t.tool_results]
        self.assertEqual([r.call_id for r in results], ["a", "m", "b"])
        self.assertTrue(results[0].content.startswith("t1 "))
        self.assertEqual(json.loads(results[1].content), {"ok": True, "id": "note_1"})
        self.assertTrue(results[2].content.startswith("t2 "))

    def test_calls_run_in_order_so_a_note_written_first_reaches_the_task(self):
        notes = []
        self.patch(dispatch, "run_agent",
                   lambda text, **kw: (notes.append("note_1" in kw["mem"].read_index()), executor(text, **kw)))
        model = ScriptedModel(
            reply(ToolCall("m", "memory_write", {"content": "use argparse"}), delegate("a", "use the note")),
            wait(),
            done(),
        )
        self.run_with(model)
        self.assertEqual(notes, [True])

    def test_orchestrator_gets_every_tool_plus_delegation(self):
        model = ScriptedModel(done())
        self.run_with(model)
        names = [t["name"] for t in model.requests[0]["tools"]]
        self.assertEqual(names, ["delegate_task", "wait_for_tasks"] + [t["name"] for t in agent.ALL_TOOLS])

    def test_read_only_session_gives_the_orchestrator_read_tools_only(self):
        model = ScriptedModel(done())
        self.run_with(model, mode=permissions.READ_ONLY)
        names = {t["name"] for t in model.requests[0]["tools"]}
        self.assertIn("read_file", names)
        self.assertFalse({"write_file", "edit_file"} & names)

    def test_orchestrator_cannot_write_a_claimed_file_until_the_task_finishes(self):
        gate = threading.Event()
        written = []

        def slow(text, **kw):
            gate.wait(5)
            executor(text, **kw)

        def release():
            gate.set()
            return wait()

        self.patch(dispatch, "run_agent", slow)
        self.file_handler("write_file", lambda args: written.append(args["path"]) or '{"success": true}')
        write_docs = ToolCall("x", "write_file", {"path": "./docs.md", "content": "x"})
        model = ScriptedModel(
            reply(delegate("a", "write the docs", lane="write", scope=["docs.md"]), write_docs),
            release,
            reply(ToolCall("y", "write_file", {"path": "docs.md", "content": "y"})),
            done(),
        )
        self.run_with(model)
        self.assertIn("claimed by t1", model.results_seen(1)[1])
        self.assertEqual(written, ["docs.md"])

    def test_overlapping_write_task_is_not_started(self):
        gate = threading.Event()

        def slow(text, **kw):
            gate.wait(5)
            executor(text, **kw)

        def release():
            gate.set()
            return wait()

        self.patch(dispatch, "run_agent", slow)
        model = ScriptedModel(
            reply(delegate("a", "docs", lane="write", scope=["docs.md"]),
                  delegate("b", "more docs", lane="write", scope=["docs.md", "api.md"])),
            release,
            done(),
        )
        self.run_with(model)
        first, second = model.results_seen(1)
        self.assertTrue(first.startswith("t1 started"))
        self.assertIn("t2 not started: docs.md already claimed", second)
        self.assertEqual(self.task_status("t2"), "failed")

    def test_a_finished_task_reaches_the_next_turn_without_waiting(self):
        delivered = threading.Event()
        real_put = dispatch.TaskInbox.put

        def put(inbox, finished):
            real_put(inbox, finished)
            delivered.set()

        self.patch(dispatch.TaskInbox, "put", put)

        def busy_until_t1_is_in_the_inbox():
            delivered.wait(5)
            return reply(ToolCall("t", "todo_write", {"todos": []}))

        model = ScriptedModel(reply(delegate("a", "look")), busy_until_t1_is_in_the_inbox, done())
        self.run_with(model)
        self.assertIn("[task finished] t1 [read] done", seen(model.requests[2]))
        self.assertNotIn("wait_for_tasks", [tc.name for r in model.requests for t in r["history"] for tc in t.tool_calls])

    def test_wait_with_nothing_running_returns_at_once(self):
        model = ScriptedModel(wait(), done())
        self.run_with(model)
        self.assertEqual(model.results_seen(1), ["No tasks are running."])

    def test_finishing_while_tasks_run_waits_and_asks_again(self):
        def slow(text, **kw):
            time.sleep(0.3)
            executor(text, **kw)

        self.patch(dispatch, "run_agent", slow)
        model = ScriptedModel(reply(delegate("a", "look around")), done("premature"), done("final"))
        final = self.run_with(model)
        self.assertEqual(final, "final")
        text = seen(model.requests[2])
        self.assertIn("[task finished] t1 [read] done", text)
        self.assertIn("cannot finish yet", text)

    def test_running_tasks_are_waited_for_when_the_turn_cap_hits(self):
        def slow(text, **kw):
            time.sleep(0.2)
            executor(text, **kw)

        self.patch(dispatch, "run_agent", slow)
        self.patch(orch, "MAX_ITERATIONS", 2)
        model = ScriptedModel(reply(delegate("a", "slow")),
                              forever=lambda: reply(ToolCall("t", "todo_write", {"todos": []})))
        self.run_with(model)
        self.assertEqual(self.task_status("t1"), "done")

    def test_running_tasks_are_waited_for_when_the_model_crashes(self):
        def slow(text, **kw):
            time.sleep(0.2)
            executor(text, **kw)

        def crash():
            raise RuntimeError("provider down")

        self.patch(dispatch, "run_agent", slow)
        model = ScriptedModel(reply(delegate("a", "slow")), crash)
        with self.assertRaises(RuntimeError):
            self.run_with(model)
        self.assertEqual(self.task_status("t1"), "done")

    def test_delivered_results_are_logged_as_an_event(self):
        model = ScriptedModel(reply(delegate("a", "look")), wait(), done())
        self.run_with(model)
        delivered = [p["content"] for e, p in self.events if e == "task_results_delivered"]
        waited = [p["content"] for e, p in self.events if e == "tool_result" and p["name"] == "wait_for_tasks"]
        self.assertIn("[task finished] t1 [read] done", "\n".join(delivered + waited))

    def test_ask_goes_to_the_approver_and_a_denial_reaches_the_model(self):
        asked = []
        approver = mock.Mock(approve=lambda name, args, reason: (asked.append(args["command"]), False)[1])
        model = ScriptedModel(reply(ToolCall("x", "run_bash_command", {"command": "pip install requests"})), done())
        self.run_with(model, approver=approver)
        self.assertEqual(asked, ["pip install requests"])
        self.assertEqual(self.bash_runs, [])
        self.assertIn("User denied", model.results_seen(1)[0])

    def test_approved_command_runs(self):
        approver = mock.Mock(approve=lambda name, args, reason: True)
        model = ScriptedModel(reply(ToolCall("x", "run_bash_command", {"command": "pip install requests"})), done())
        self.run_with(model, approver=approver)
        self.assertEqual(self.bash_runs, ["pip install requests"])

    def test_without_an_approver_asks_are_refused(self):
        model = ScriptedModel(reply(ToolCall("x", "run_bash_command", {"command": "pip install requests"})), done())
        self.run_with(model)
        self.assertEqual(self.bash_runs, [])
        self.assertIn("User denied", model.results_seen(1)[0])

    def test_read_only_session_refuses_the_orchestrators_own_write(self):
        written = []
        self.file_handler("write_file", lambda args: written.append(args) or '{"ok": true}')
        model = ScriptedModel(reply(ToolCall("w", "write_file", {"path": "cli.py", "content": "x"})), done())
        self.run_with(model, mode=permissions.READ_ONLY)
        self.assertEqual(written, [])
        self.assertIn("Refused", model.results_seen(1)[0])

    def test_read_only_session_offers_only_read_and_refuses_write(self):
        model = ScriptedModel(reply(delegate("a", "fix it", lane="write", scope=["cli.py"])), wait(), done())
        self.run_with(model, mode=permissions.READ_ONLY)
        [tool] = [t for t in model.requests[0]["tools"] if t["name"] == "delegate_task"]
        self.assertEqual(tool["parameters"]["properties"]["lane"]["enum"], ["read"])
        self.assertIn("read-only", model.requests[0]["prompt"])
        self.assertIn("[task finished] t1 [write] failed: t1 refused", seen(model.requests[2]))
        self.assertEqual(self.executor_calls, [])

    def test_bad_delegation_is_reported_and_the_rest_still_run(self):
        model = ScriptedModel(
            reply(ToolCall("bad", "delegate_task", {"lane": "read"}), delegate("ok", "valid")),
            wait(),
            done(),
        )
        self.run_with(model)
        bad, ok = model.results_seen(1)
        self.assertIn("Task not created", bad)
        self.assertTrue(ok.startswith("t1 started"))
        self.assertIn("[task finished] t1 [read] done", seen(model.requests[2]))

    def test_a_write_task_without_a_scope_is_not_created(self):
        model = ScriptedModel(reply(delegate("w", "fix whatever is broken", lane="write")), done())
        self.run_with(model)
        (result,) = model.results_seen(1)
        self.assertIn("Task not created", result)
        self.assertIn("scope", result)
        self.assertEqual(self.executor_calls, [])

    def test_unknown_and_unparseable_calls_get_errors(self):
        model = ScriptedModel(
            reply(ToolCall("x", "launch_rockets", {}),
                  ToolCall("y", "todo_write", {}, raw={"args_parse_error": "bad", "raw_arguments": "{"})),
            done(),
        )
        self.run_with(model)
        unknown, broken = model.results_seen(1)
        self.assertIn("Unknown tool: launch_rockets", unknown)
        self.assertIn("Could not parse", broken)

    def test_turn_cap_stops_a_looping_orchestrator(self):
        model = ScriptedModel(forever=lambda: reply(ToolCall("t", "todo_write", {"todos": []})))
        self.run_with(model)
        self.assertEqual(len(model.requests), orch.MAX_ITERATIONS)
        self.assertIn("error", [e for e, _ in self.events])

    def test_compaction_keeps_every_task(self):
        model = ScriptedModel(
            reply(delegate("a", "first")),
            wait("w1"),
            reply(delegate("b", "second")),
            wait("w2"),
            done(),
        )
        sizes = iter([10, 10, 10, 10**9])
        self.patch(context, "history_chars", lambda history: next(sizes))
        self.run_with(model)
        [only] = model.requests[4]["history"]
        self.assertEqual(only.role, "user")
        self.assertIn("Every task so far", only.text)
        self.assertIn("t1 [read] done", only.text)
        self.assertIn("t2 [read] done", only.text)

    def test_a_small_history_is_never_compacted_however_many_turns(self):
        steps = [reply(ToolCall(f"r{i}", "todo_write", {"todos": []})) for i in range(15)]
        model = ScriptedModel(*steps, done())
        self.run_with(model)
        self.assertEqual(model.requests[-1]["history"][0].text, "add a --verbose flag")
        self.assertEqual(len(model.requests[-1]["history"]), 31)

    def test_injected_message_reaches_the_orchestrator(self):
        queue = InjectionQueue()
        queue.push(None, "use click, not argparse")
        model = ScriptedModel(done())
        self.run_with(model, injection_queue=queue)
        texts = [t.text for t in model.requests[0]["history"]]
        self.assertIn("[Message from human]: use click, not argparse", texts)

    def test_final_text_is_the_last_thing_said(self):
        model = ScriptedModel(reply(delegate("a", "x"), text="Starting."), wait(), done("Finished."))
        self.assertEqual(self.run_with(model), "Finished.")

    def test_picks_a_strong_model_when_none_is_given(self):
        self.patch(orch.routing, "pick_orchestrator", lambda: "claude-opus")
        chosen = []
        model = ScriptedModel(done())
        self.patch(orch, "get_provider", lambda key: (chosen.append(key), model)[1])
        orch.run_orchestrator("x", mem=self.mem)
        self.assertEqual(chosen, ["claude-opus"])

    def test_proposals_reach_the_next_turn_on_the_right_task(self):
        def executor_that_proposes(text, **kw):
            if "flag" in text:
                kw["extra_handlers"]["ask_orchestrator"]({"proposal": "README documents the old flags"})
                kw["extra_handlers"]["ask_orchestrator"]({"proposal": "tests/ has no cli tests"})
            executor(text, **kw)
        self.patch(dispatch, "run_agent", executor_that_proposes)
        model = ScriptedModel(
            reply(delegate("a", "look around"), delegate("b", "add the flag", lane="write", scope=["cli.py"])),
            wait(), wait("w2"),
            done(),
        )
        self.run_with(model)
        lines = [l for l in seen(model.requests[-1]).splitlines() if l.startswith("[task finished]")]
        quiet = [l for l in lines if " t1 " in l]
        proposing = [l for l in lines if " t2 " in l]
        self.assertEqual(len(quiet), 1)
        self.assertNotIn("proposes", quiet[0])
        self.assertEqual(proposing, ["[task finished] t2 [write] done: finished: add the flag | changed cli.py"
                                     " | proposes: README documents the old flags; tests/ has no cli tests"])

    def test_a_failed_task_can_still_propose(self):
        def gives_up(text, **kw):
            kw["extra_handlers"]["ask_orchestrator"]({"proposal": "split this task"})
        self.patch(dispatch, "run_agent", gives_up)
        model = ScriptedModel(reply(delegate("a", "too big")), wait(), done())
        self.run_with(model)
        self.assertIn("[task finished] t1 [read] failed: ended without calling report | proposes: split this task",
                      seen(model.requests[2]))

    def test_prompt_explains_proposals(self):
        model = ScriptedModel(done())
        self.run_with(model)
        self.assertIn("proposes:", model.requests[0]["prompt"])

    def test_unknown_mode_fails_before_any_model_call(self):
        model = ScriptedModel(done())
        with self.assertRaises(ValueError):
            self.run_with(model, mode="yolo")
        self.assertEqual(model.requests, [])


if __name__ == "__main__":
    unittest.main()
