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
from orchestration.executor_tools import (
    EXECUTOR_TOOLS, REPORT_TOOL, Proposal, ProposalBox, ReportCollector, parse_report,
)
from orchestration.policy import READ_LANE_TOOLS
from orchestration.tasks import Report
from providers import ProviderResponse, ToolCall

GOOD = {"outcome": "added --verbose", "changes": ["cli.py"], "validation": "ran --help", "blockers": []}


class ParseReport(unittest.TestCase):
    def test_well_formed(self):
        self.assertEqual(parse_report(GOOD), Report("added --verbose", ["cli.py"], "ran --help", []))

    def test_missing_or_empty_text_is_rejected(self):
        for field in ("outcome", "validation"):
            for bad in (None, "", "   ", 3):
                with self.subTest(field=field, bad=bad):
                    args = dict(GOOD)
                    if bad is None:
                        del args[field]
                    else:
                        args[field] = bad
                    with self.assertRaises(ValueError) as ctx:
                        parse_report(args)
                    self.assertIn(field, str(ctx.exception))

    def test_lists_tolerate_a_bare_string(self):
        r = parse_report({**GOOD, "changes": "cli.py", "blockers": ""})
        self.assertEqual((r.changes, r.blockers), (["cli.py"], []))

    def test_missing_lists_default_to_empty(self):
        args = {"outcome": "looked around", "validation": "none"}
        self.assertEqual(parse_report(args).changes, [])

    def test_lists_reject_non_strings(self):
        for bad in ([1, 2], {"a": 1}, 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_report({**GOOD, "changes": bad})

    def test_whitespace_is_trimmed(self):
        r = parse_report({**GOOD, "outcome": "  done  ", "changes": [" a.py ", "  "]})
        self.assertEqual((r.outcome, r.changes), ("done", ["a.py"]))


class Collector(unittest.TestCase):
    def test_accepts_once(self):
        c = ReportCollector()
        self.assertFalse(c.done)
        self.assertEqual(json.loads(c.handle(GOOD)), {"ok": True})
        self.assertTrue(c.done)
        first = c.report
        self.assertIn("error", json.loads(c.handle({**GOOD, "outcome": "second"})))
        self.assertIs(c.report, first)

    def test_bad_report_leaves_it_open_for_a_retry(self):
        c = ReportCollector()
        self.assertIn("error", json.loads(c.handle({"outcome": ""})))
        self.assertFalse(c.done)
        c.handle(GOOD)
        self.assertTrue(c.done)

    def test_handlers_route_report(self):
        c = ReportCollector()
        c.handlers()["report"](GOOD)
        self.assertTrue(c.done)


class Wiring(unittest.TestCase):
    def test_report_needs_no_approval(self):
        for mode in permissions.MODES:
            self.assertEqual(permissions.classify("report", GOOD, mode).action, permissions.ALLOW)

    def test_readers_receive_report(self):
        self.assertIn("report", READ_LANE_TOOLS)

    def test_ask_orchestrator_needs_no_approval_and_readers_get_it(self):
        for mode in permissions.MODES:
            self.assertEqual(permissions.classify("ask_orchestrator", {}, mode).action, permissions.ALLOW)
        self.assertIn("ask_orchestrator", READ_LANE_TOOLS)

    def test_executor_tools_are_named_consistently(self):
        self.assertEqual([t["name"] for t in EXECUTOR_TOOLS], ["report", "ask_orchestrator"])
        self.assertEqual(set(REPORT_TOOL["parameters"]["required"]), set(GOOD))


class Proposals(unittest.TestCase):
    def test_proposal_is_tagged_with_the_handlers_task(self):
        box = ProposalBox()
        box.handler_for("t2")({"proposal": "  README documents the old flags  "})
        self.assertEqual(box.drain(), [Proposal("t2", "README documents the old flags")])

    def test_empty_proposal_is_refused(self):
        box = ProposalBox()
        for bad in ({}, {"proposal": "  "}, {"proposal": 3}):
            self.assertIn("error", json.loads(box.handler_for("t1")(bad)))
        self.assertEqual(box.drain(), [])

    def test_drain_empties_the_box(self):
        box = ProposalBox()
        box.handler_for("t1")({"proposal": "x"})
        self.assertEqual(len(box.drain()), 1)
        self.assertEqual(box.drain(), [])

    def test_proposal_is_announced(self):
        seen = []
        box = ProposalBox(lambda e, p: seen.append((e, p)))
        box.handler_for("t3")({"proposal": "fix the typo in setup.py"})
        self.assertEqual(seen, [("proposal_made", {"task_id": "t3", "text": "fix the typo in setup.py"})])

    def test_concurrent_proposals_are_all_kept(self):
        import threading
        box = ProposalBox()
        threads = [threading.Thread(target=box.handler_for(f"t{i}"), args=({"proposal": f"idea {i}"},))
                   for i in range(64)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(sorted(p.task_id for p in box.drain()), sorted(f"t{i}" for i in range(64)))


class FakeProvider:
    def __init__(self, script):
        self.script = list(script)
        self.seen_tools = []

    def generate(self, history, tools, system_prompt):
        self.seen_tools.append([t["name"] for t in tools])
        return self.script.pop(0) if self.script else ProviderResponse(text="(script exhausted)", tool_calls=[])


def call(name, args, call_id="c1"):
    return ProviderResponse(text=None, tool_calls=[ToolCall(id=call_id, name=name, args=args)])


class RunAgentHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = memory.Memory(Path(self.tmp.name) / "mem.md")

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, script, **kwargs):
        fake = FakeProvider(script)
        with mock.patch.object(agent, "get_provider", return_value=fake):
            text = agent.run_agent("task", verbose=False, interactive=False, mem=self.mem, **kwargs)
        return fake, text

    def test_default_tools_unchanged(self):
        fake, text = self.run_with([ProviderResponse(text="hi", tool_calls=[])])
        self.assertEqual(fake.seen_tools, [[t["name"] for t in agent.ALL_TOOLS]])
        self.assertEqual(text, "hi")

    def test_custom_tools_reach_the_model(self):
        fake, _ = self.run_with([ProviderResponse(text="ok", tool_calls=[])],
                                tools=[REPORT_TOOL])
        self.assertEqual(fake.seen_tools, [["report"]])

    def test_report_ends_the_run(self):
        c = ReportCollector()
        script = [call("report", GOOD), ProviderResponse(text="should never be asked", tool_calls=[])]
        fake, _ = self.run_with(script, tools=agent.ALL_TOOLS + EXECUTOR_TOOLS,
                                extra_handlers=c.handlers(), stop_when=lambda: c.done)
        self.assertEqual(c.report.outcome, "added --verbose")
        self.assertEqual(len(fake.seen_tools), 1)
        self.assertEqual(len(fake.script), 1)

    def test_rejected_report_lets_the_model_retry(self):
        c = ReportCollector()
        script = [call("report", {"outcome": ""}, "c1"), call("report", GOOD, "c2")]
        fake, _ = self.run_with(script, tools=EXECUTOR_TOOLS,
                                extra_handlers=c.handlers(), stop_when=lambda: c.done)
        self.assertTrue(c.done)
        self.assertEqual(len(fake.seen_tools), 2)

    def test_ending_without_a_report_leaves_collector_empty(self):
        c = ReportCollector()
        self.run_with([ProviderResponse(text="all done!", tool_calls=[])], tools=EXECUTOR_TOOLS,
                      extra_handlers=c.handlers(), stop_when=lambda: c.done)
        self.assertFalse(c.done)

    def test_extra_handlers_take_priority(self):
        seen = []
        self.run_with([call("memory_read", {"entry_id": "x"}), ProviderResponse(text="ok", tool_calls=[])],
                      extra_handlers={"memory_read": lambda a: seen.append(a) or "{}"})
        self.assertEqual(seen, [{"entry_id": "x"}])


if __name__ == "__main__":
    unittest.main()
