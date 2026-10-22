import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
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
            return self.responses.pop(0)
        return self.forever() if self.forever else done("(script exhausted)")

    def results_seen(self, n):
        return [r.content for r in self.requests[n]["history"][-1].tool_results]


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

    def run_with(self, model, mode=permissions.WORKSPACE_WRITE, **kw):
        self.patch(orch, "get_provider", lambda key: model)
        kw.setdefault("model_key", "claude-opus")
        return orch.run_orchestrator("add a --verbose flag", mem=self.mem, mode=mode,
                                     event_sink=lambda e, p: self.events.append((e, p)), **kw)

    def test_dependency_flows_through_the_conversation(self):
        model = ScriptedModel(
            reply(delegate("a", "find the env loader")),
            reply(delegate("b", "add --verbose using what t1 found", lane="write", scope=["cli.py"])),
            done("Added --verbose."),
        )
        final = self.run_with(model)
        self.assertEqual(final, "Added --verbose.")
        self.assertEqual(model.results_seen(1), ["t1 [read] done: finished: find the env loader"])
        self.assertEqual(model.results_seen(2),
                         ["t2 [write] done: finished: add --verbose using what t1 found | changed cli.py"])

    def test_one_reply_is_one_batch_and_ids_continue(self):
        batches = []
        real = orch.run_batch

        def counting_run_batch(tasks, *a, **k):
            batches.append([t.id for t in tasks])
            return real(tasks, *a, **k)

        self.patch(orch, "run_batch", counting_run_batch)
        model = ScriptedModel(
            reply(delegate("a", "one"), delegate("b", "two")),
            reply(delegate("c", "three")),
            done(),
        )
        self.run_with(model)
        self.assertEqual(batches, [["t1", "t2"], ["t3"]])

    def test_results_come_back_in_call_order(self):
        model = ScriptedModel(
            reply(delegate("a", "first"),
                  ToolCall("m", "memory_write", {"content": "note"}),
                  delegate("b", "second")),
            done(),
        )
        self.run_with(model)
        results = model.requests[1]["history"][-1].tool_results
        self.assertEqual([r.call_id for r in results], ["a", "m", "b"])
        self.assertTrue(results[0].content.startswith("t1 "))
        self.assertEqual(json.loads(results[1].content), {"ok": True, "id": "note_1"})
        self.assertTrue(results[2].content.startswith("t2 "))

    def test_memory_note_lands_before_the_batch_runs(self):
        seen = []
        self.patch(dispatch, "run_agent",
                   lambda text, **kw: (seen.append("note_1" in kw["mem"].read_index()), executor(text, **kw)))
        model = ScriptedModel(
            reply(delegate("a", "use the note"), ToolCall("m", "memory_write", {"content": "use argparse"})),
            done(),
        )
        self.run_with(model)
        self.assertEqual(seen, [True])

    def test_orchestrator_gets_every_tool_plus_delegation(self):
        model = ScriptedModel(done())
        self.run_with(model)
        names = [t["name"] for t in model.requests[0]["tools"]]
        self.assertEqual(names, ["delegate_task"] + [t["name"] for t in agent.ALL_TOOLS])

    def test_read_only_session_gives_the_orchestrator_read_tools_only(self):
        model = ScriptedModel(done())
        self.run_with(model, mode=permissions.READ_ONLY)
        names = {t["name"] for t in model.requests[0]["tools"]}
        self.assertIn("read_file", names)
        self.assertFalse({"write_file", "edit_file"} & names)

    def test_own_write_runs_before_the_batch(self):
        order = []
        self.patch(dispatch, "run_agent", lambda text, **kw: (order.append("executor"), executor(text, **kw)))
        self.file_handler("write_file", lambda args: (order.append("write"), '{"ok": true}')[1])
        model = ScriptedModel(
            reply(delegate("a", "check it"), ToolCall("w", "write_file", {"path": "cli.py", "content": "x"})),
            done(),
        )
        self.run_with(model)
        self.assertEqual(order, ["write", "executor"])
        self.assertEqual(model.results_seen(1)[1], '{"ok": true}')

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
        model = ScriptedModel(reply(delegate("a", "fix it", lane="write", scope=["cli.py"])), done())
        self.run_with(model, mode=permissions.READ_ONLY)
        [tool] = [t for t in model.requests[0]["tools"] if t["name"] == "delegate_task"]
        self.assertEqual(tool["parameters"]["properties"]["lane"]["enum"], ["read"])
        self.assertIn("read-only", model.requests[0]["prompt"])
        [result] = model.results_seen(1)
        self.assertTrue(result.startswith("t1 [write] failed: t1 refused"))
        self.assertEqual(self.executor_calls, [])

    def test_bad_delegation_is_reported_and_the_rest_still_run(self):
        model = ScriptedModel(
            reply(ToolCall("bad", "delegate_task", {"lane": "read"}), delegate("ok", "valid")),
            done(),
        )
        self.run_with(model)
        bad, ok = model.results_seen(1)
        self.assertIn("Task not created", bad)
        self.assertTrue(ok.startswith("t1 [read] done"))

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
        self.patch(orch, "COMPACT_EVERY", 2)
        model = ScriptedModel(
            reply(delegate("a", "first")),
            reply(delegate("b", "second")),
            done(),
        )
        self.run_with(model)
        [only] = model.requests[2]["history"]
        self.assertEqual(only.role, "user")
        self.assertIn("Every task so far", only.text)
        self.assertIn("t1 [read] done", only.text)
        self.assertIn("t2 [read] done", only.text)

    def test_injected_message_reaches_the_orchestrator(self):
        queue = InjectionQueue()
        queue.push(None, "use click, not argparse")
        model = ScriptedModel(done())
        self.run_with(model, injection_queue=queue)
        texts = [t.text for t in model.requests[0]["history"]]
        self.assertIn("[Message from human]: use click, not argparse", texts)

    def test_final_text_is_the_last_thing_said(self):
        model = ScriptedModel(reply(delegate("a", "x"), text="Starting."), done("Finished."))
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
            done(),
        )
        self.run_with(model)
        quiet, proposing = model.results_seen(1)
        self.assertNotIn("proposes", quiet)
        self.assertTrue(proposing.startswith("t2 [write] done"))
        self.assertTrue(proposing.endswith("proposes: README documents the old flags; tests/ has no cli tests"))

    def test_a_failed_task_can_still_propose(self):
        def gives_up(text, **kw):
            kw["extra_handlers"]["ask_orchestrator"]({"proposal": "split this task"})
        self.patch(dispatch, "run_agent", gives_up)
        model = ScriptedModel(reply(delegate("a", "too big")), done())
        self.run_with(model)
        [result] = model.results_seen(1)
        self.assertEqual(result, "t1 [read] failed: ended without calling report | proposes: split this task")

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
