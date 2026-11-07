import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
import permissions
from orchestration import orchestrator
from orchestration.policy import READ_LANE_TOOLS
from tools.board_tool import BOARD_NOTE, board


def error(result):
    try:
        return json.loads(result).get("error")
    except ValueError:
        return None


class Cards(unittest.TestCase):
    def test_a_card_with_a_diagram_and_a_note(self):
        result = board({"title": "Flow", "diagram": "graph LR\n  A --> B", "note": "**why**"})
        self.assertIsNone(error(result))
        self.assertIn('"Flow"', result)
        self.assertIn("ctrl+b", result)

    def test_every_supported_kind(self):
        for diagram in ("graph TD\n A-->B", "flowchart LR\n A-->B", "%% c\nsequenceDiagram\n A->>B: hi",
                        "graph LR; A-->B"):
            self.assertIsNone(error(board({"diagram": diagram})), diagram)

    def test_unsupported_kinds_are_refused_with_what_to_do(self):
        message = error(board({"diagram": "pie\n  \"a\": 1"}))
        self.assertIn("not pie", message)
        self.assertIn("note", message)

    def test_a_fenced_diagram_is_refused(self):
        self.assertIn("without the ``` fence", error(board({"diagram": "```mermaid\ngraph LR\nA-->B\n```"})))

    def test_an_empty_card_is_refused_but_clear_is_fine(self):
        self.assertIn("needs a title", error(board({})))
        self.assertEqual(board({"clear": True}), "Cleared the board.")

    def test_size_limits(self):
        self.assertIn("Split it", error(board({"diagram": "graph LR\n" + "A-->B\n" * 1000})))
        self.assertIn("Split it", error(board({"note": "x" * 7000})))


class Wiring(unittest.TestCase):
    def test_every_agent_has_the_tool_and_it_never_asks(self):
        self.assertIn("board", [t["name"] for t in agent.ALL_TOOLS])
        self.assertEqual(permissions.classify("board", {"title": "x"}, permissions.READ_ONLY).action,
                         permissions.ALLOW)
        self.assertIn("board", READ_LANE_TOOLS)

    def test_execute_tool_routes_it(self):
        result = agent.execute_tool("board", {"title": "x"}, "m", None, None)
        self.assertIn("Added", result)

    def test_both_prompts_explain_the_board(self):
        self.assertIn(BOARD_NOTE, agent.build_system_prompt("m"))
        run = SimpleNamespace(model_key="m", mode=permissions.WORKSPACE_WRITE)
        self.assertIn(BOARD_NOTE, orchestrator._system_prompt(run))


if __name__ == "__main__":
    unittest.main()
