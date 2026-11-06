import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
import memory
import permissions
from history import History
from orchestration import dispatch
from orchestration import orchestrator as orch
from providers import ProviderResponse, ToolCall
from store import Store
from store.history_log import Journal
from tools.todo_tool import TodoManager

ECHO = {"type": "function", "name": "echo", "description": "Echo the text.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}


class Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, history, tools, system_prompt):
        self.calls += 1
        return self.responses.pop(0) if self.responses else ProviderResponse(text="(script exhausted)", tool_calls=[])


def calls(*pairs, text=None):
    return ProviderResponse(text=text, tool_calls=[ToolCall(id=f"c{i}", name=name, args=args)
                                                   for i, (name, args) in enumerate(pairs)])


class RecordedRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store.open(Path(self.tmp.name) / "fox.db")
        self.addCleanup(self.store.close)
        self.store.append_event("s1", "session_started", {
            "task": "t", "workspace": self.tmp.name, "mode": "agent", "permission_mode": "workspace-write",
            "model_key": "claude-haiku", "parent_id": None})
        self.journal = Journal(self.store, "s1")
        self.sink = lambda event_type, payload: self.store.append_event("s1", event_type, payload)

    def test_an_agent_run_rebuilds_to_the_live_history(self):
        model = Scripted(
            calls(("echo", {"text": "one"}), ("echo", {"text": "two"}), text="Echoing twice."),
            calls(("todo_write", {"todos": [{"content": "ship it", "status": "in_progress"}]})),
            ProviderResponse(text="Done.", tool_calls=[]),
        )
        live = History("say things", self.journal.recorder(None))
        with mock.patch.object(agent, "get_provider", return_value=model):
            agent.run_agent("say things", verbose=False, interactive=False, model_key="claude-haiku",
                            mem=memory.Memory.scratch(), tools=agent.ALL_TOOLS + [ECHO], history=live,
                            extra_handlers={"echo": lambda args: f"echo: {args['text']}"},
                            mode=permissions.FULL_ACCESS)
        rebuilt = self.journal.turns()
        self.assertEqual(rebuilt, live.turns)
        self.assertEqual([r.content for r in rebuilt[2].tool_results], ["echo: one", "echo: two"])

    def test_the_todo_list_comes_back_from_the_history(self):
        model = Scripted(calls(("todo_write", {"todos": [{"content": "fix total", "status": "completed"},
                                                         {"content": "add tests", "status": "in_progress"}]})),
                         ProviderResponse(text="Done.", tool_calls=[]))
        with mock.patch.object(agent, "get_provider", return_value=model):
            agent.run_agent("plan", verbose=False, interactive=False, model_key="claude-haiku",
                            mem=memory.Memory.scratch(), recorder=self.journal.recorder(None))
        todos = TodoManager()
        agent.restore_todos(todos, self.journal.turns())
        self.assertIn("add tests", todos.render())
        self.assertIn("fix total", todos.render())

    def test_a_resumed_run_starts_from_the_rebuilt_history(self):
        first = Scripted(ProviderResponse(text="First answer.", tool_calls=[]))
        with mock.patch.object(agent, "get_provider", return_value=first):
            agent.run_agent("question", verbose=False, interactive=False, model_key="claude-haiku",
                            mem=memory.Memory.scratch(), recorder=self.journal.recorder(None))
        seen = []

        class Watching(Scripted):
            def generate(self, history, tools, system_prompt):
                seen.append([t.text for t in history])
                return super().generate(history, tools, system_prompt)

        resumed = History(turns=self.journal.turns(), recorder=self.journal.recorder(None))
        resumed.add(agent.Turn(role="user", text="[Message from human]: and then?"))
        with mock.patch.object(agent, "get_provider", return_value=Watching(ProviderResponse(text="Second.",
                                                                                              tool_calls=[]))):
            agent.run_agent("question", verbose=False, interactive=False, model_key="claude-haiku",
                            mem=memory.Memory.scratch(), history=resumed)
        self.assertEqual(seen[0], ["question", "[Message from human]: and then?"])
        self.assertEqual([t.text for t in self.journal.turns()], ["question", "[Message from human]: and then?"])

    def test_an_orchestrator_and_its_executor_each_get_a_history(self):
        orchestrator_model = Scripted(
            calls(("delegate_task", {"description": "look at the cart", "lane": "read", "scope": []})),
            calls(("wait_for_tasks", {})),
            ProviderResponse(text="All done.", tool_calls=[]),
        )
        executor_model = Scripted(calls(("report", {"outcome": "the cart is fine", "changes": [],
                                                    "validation": "read it", "blockers": []})))
        live = History("check the cart", self.journal.recorder(None), notes_seen=0)
        with mock.patch.object(orch, "get_provider", return_value=orchestrator_model), \
                mock.patch.object(agent, "get_provider", return_value=executor_model), \
                mock.patch.object(dispatch.routing, "pick_executor", lambda preferred=None: "claude-haiku"):
            orch.run_orchestrator("check the cart", model_key="claude-haiku", mem=memory.Memory.scratch(),
                                  journal=self.journal, history=live)
        self.assertEqual(self.journal.agents(), [None, "t1"])
        self.assertEqual(self.journal.turns(), live.turns)
        executor_turns = self.journal.turns("t1")
        self.assertEqual(executor_turns[-2].tool_calls[0].name, "report")
        self.assertIn("[task finished] t1", self.journal.turns()[-1].tool_results[0].content)
        self.assertEqual(self.journal.meta("notes_seen"), 0)


    def report(self, outcome):
        return calls(("report", {"outcome": outcome, "changes": [], "validation": "read it", "blockers": []}))

    def test_the_orchestrator_continues_a_finished_task_with_its_history(self):
        seen = []

        class Watching(Scripted):
            def generate(self, history, tools, system_prompt):
                seen.append([t.text for t in history if t.text])
                return super().generate(history, tools, system_prompt)

        executors = [Scripted(self.report("cart is fine")), Watching(self.report("empty cart handled too"))]
        orchestrator_model = Scripted(
            calls(("delegate_task", {"description": "look at the cart", "lane": "read", "scope": []})),
            calls(("wait_for_tasks", {})),
            calls(("continue_task", {"task_id": "t1", "message": "check the empty cart"})),
            calls(("wait_for_tasks", {})),
            ProviderResponse(text="All done.", tool_calls=[]),
        )
        with mock.patch.object(orch, "get_provider", return_value=orchestrator_model),                 mock.patch.object(agent, "get_provider", side_effect=lambda key: executors.pop(0)),                 mock.patch.object(dispatch.routing, "pick_executor", lambda preferred=None: "claude-haiku"):
            orch.run_orchestrator("check the cart", model_key="claude-haiku", mem=memory.Memory.scratch(),
                                  journal=self.journal, event_sink=self.sink)
        self.assertTrue(seen[0][0].startswith("look at the cart"))
        self.assertEqual(seen[0][-1], "[Message from the orchestrator]: check the empty cart")
        results = [r.content for t in self.journal.turns() for r in t.tool_results]
        self.assertIn("t1 continues (run 2). Its result will arrive as a message.", results)
        self.assertTrue(any("[task finished] t1 [read] done: empty cart handled too (run 2)" in r for r in results))
        task = self.store._query("SELECT status, run FROM tasks")[0]
        self.assertEqual(task, {"status": "done", "run": 2})

    def test_continuing_needs_a_finished_task(self):
        orchestrator_model = Scripted(
            calls(("continue_task", {"task_id": "t9", "message": "go"})),
            ProviderResponse(text="ok", tool_calls=[]),
        )
        with mock.patch.object(orch, "get_provider", return_value=orchestrator_model):
            orch.run_orchestrator("x", model_key="claude-haiku", mem=memory.Memory.scratch(), journal=self.journal)
        results = [r.content for t in self.journal.turns() for r in t.tool_results]
        self.assertIn("there is no task t9", results[0])

    def test_a_human_message_reopens_a_finished_executor(self):
        hooks = []
        executors = [Scripted(self.report("cart is fine")), Scripted(self.report("added the empty case"))]
        orchestrator_model = Scripted(
            calls(("delegate_task", {"description": "look at the cart", "lane": "read", "scope": []})),
            calls(("wait_for_tasks", {})),
            ProviderResponse(text="All done.", tool_calls=[]),
        )
        with mock.patch.object(orch, "get_provider", return_value=orchestrator_model),                 mock.patch.object(agent, "get_provider", side_effect=lambda key: executors.pop(0)),                 mock.patch.object(dispatch.routing, "pick_executor", lambda preferred=None: "claude-haiku"):
            orch.run_orchestrator("check the cart", model_key="claude-haiku", mem=memory.Memory.scratch(),
                                  journal=self.journal, on_reopen=hooks.append, event_sink=self.sink)
            self.assertIsNone(hooks[0]("t1", "also the empty cart please"))
            for _ in range(100):
                if self.store._query("SELECT status FROM tasks")[0]["status"] == "done" and not executors:
                    break
                time.sleep(0.05)
        texts = [t.text for t in self.journal.turns("t1") if t.text]
        self.assertEqual(texts[-1], "[Message from human]: also the empty cart please")
        self.assertEqual(self.store._query("SELECT run FROM tasks")[0]["run"], 2)


if __name__ == "__main__":
    unittest.main()
