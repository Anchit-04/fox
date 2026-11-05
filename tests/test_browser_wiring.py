import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import permissions
from approval import HANDOFF_ENDED, ApprovalGate, DenyApprover
from history import IMAGE_DROPPED, History
from providers import Image, ToolResult, Turn, has_vision
from providers.anthropic import _to_anthropic_messages
from providers.gemini import _to_gemini_contents
from providers.openai_compatible import _to_openai_messages
from tools import browser_tools

PIXEL = Image("image/jpeg", "aGVsbG8=")


class Policy(unittest.TestCase):
    def test_node_scripts_now_ask(self):
        self.assertEqual(permissions.classify("run_bash_command", {"command": "node fetch.js"}).action, permissions.ASK)
        self.assertEqual(permissions.classify("run_bash_command", {"command": "node --version"}).action,
                         permissions.ALLOW)

    def test_browsing_is_free_in_every_mode(self):
        for mode in permissions.MODES:
            for name in ("browser_open", "browser_click", "browser_type", "browser_ask_human"):
                self.assertEqual(permissions.classify(name, {}, mode).action, permissions.ALLOW, (name, mode))

    def test_uploads_ask_and_read_only_refuses_them(self):
        decision = permissions.classify("browser_upload", {"files": ["photo.png"]})
        self.assertEqual(decision.action, permissions.ASK)
        self.assertIn("photo.png", decision.reason)
        self.assertEqual(permissions.classify("browser_upload", {"files": ["a"]}, permissions.READ_ONLY).action,
                         permissions.DENY)
        self.assertEqual(permissions.classify("browser_upload", {"files": ["a"]}, permissions.FULL_ACCESS).action,
                         permissions.ALLOW)

    def test_every_browser_tool_has_a_policy(self):
        self.assertEqual(browser_tools.NAMES, permissions.BROWSER_TOOLS)


class Handoffs(unittest.TestCase):
    def test_the_gate_announces_a_handoff_without_blocking(self):
        events = []
        gate = ApprovalGate(emit=lambda kind, payload: events.append(payload))
        request = gate.start_handoff(kind="login", message="sign in on x.test", url="https://x.test/", task_id="t3")
        self.assertEqual(events[0]["kind"], "handoff")
        self.assertEqual(events[0]["task_id"], "t3")
        self.assertEqual(events[0]["request_id"], request)
        self.assertIsNone(gate.handoff_answer(request))
        gate.respond(request, False, always=True, tool_name="browser_ask_human")
        self.assertIs(gate.handoff_answer(request), False)
        self.assertNotIn("browser_ask_human", gate._always)
        gate.finish_handoff(request)

    def test_a_closed_gate_ends_the_handoff(self):
        gate = ApprovalGate()
        request = gate.start_handoff(kind="ask", message="help")
        gate.close()
        self.assertEqual(gate.handoff_answer(request), HANDOFF_ENDED)
        self.assertIsNone(gate.start_handoff(kind="ask", message="help"))
        self.assertIsNone(DenyApprover().start_handoff(kind="ask", message="help"))


class Images(unittest.TestCase):
    def turns(self):
        return [Turn(role="user", text="go"),
                Turn(role="user", tool_results=[ToolResult(call_id="c1", name="browser_screenshot",
                                                           content="shot", images=[PIXEL])]),
                Turn(role="user", text="what is this?", images=[PIXEL])]

    def test_anthropic_puts_images_inside_the_tool_result(self):
        messages = _to_anthropic_messages(self.turns())
        block = messages[1]["content"][0]
        self.assertEqual(block["content"][0], {"type": "text", "text": "shot"})
        self.assertEqual(block["content"][1]["source"]["data"], PIXEL.data)
        self.assertEqual(messages[2]["content"][1]["type"], "image")

    def test_openai_adds_a_user_message_after_the_tool_messages(self):
        messages = _to_openai_messages(self.turns())
        self.assertEqual(messages[1]["role"], "tool")
        self.assertEqual(messages[2]["role"], "user")
        self.assertTrue(messages[2]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(messages[3]["content"][1]["type"], "image_url")

    def test_gemini_adds_inline_image_parts(self):
        contents = _to_gemini_contents(self.turns())
        self.assertEqual(contents[1].parts[1].inline_data.data, b"hello")
        self.assertEqual(contents[2].parts[1].inline_data.mime_type, "image/jpeg")

    def test_history_keeps_only_the_newest_screenshots(self):
        history = History("task")
        for i in range(4):
            history.add(Turn(role="user", tool_results=[ToolResult(call_id=f"c{i}", name="browser_screenshot",
                                                                   content=f"shot {i}", images=[PIXEL])]))
        history.keep_latest_images(2)
        results = [t.tool_results[0] for t in history.turns[1:]]
        self.assertEqual([len(r.images) for r in results], [0, 0, 1, 1])
        self.assertTrue(results[0].content.endswith(IMAGE_DROPPED))
        history.keep_latest_images(2)
        self.assertEqual(results[0].content.count(IMAGE_DROPPED), 1)

    def test_vision_flags(self):
        self.assertTrue(has_vision("claude-haiku"))
        self.assertTrue(has_vision("nemotron-super"))
        self.assertFalse(has_vision("deepseek-chat"))
        self.assertFalse(has_vision("not-a-model"))


class ToolLists(unittest.TestCase):
    def names(self, session):
        return [t["name"] for t in session.tools()]

    def test_only_open_shows_until_a_page_is_open(self):
        self.assertEqual(self.names(browser_tools.BrowserSession("deepseek-chat")), ["browser_open"])

    def test_seeing_models_get_screenshots_and_blind_ones_do_not(self):
        seeing = browser_tools.BrowserSession("claude-haiku")
        blind = browser_tools.BrowserSession("deepseek-chat")
        for session in (seeing, blind):
            session.actions = object()
        self.assertIn("browser_screenshot", self.names(seeing))
        self.assertNotIn("browser_screenshot", self.names(blind))
        self.assertIn("browser_layout_check", self.names(blind))
        self.assertNotIn("browser_look", self.names(blind))

    def test_read_only_sessions_have_no_upload(self):
        session = browser_tools.BrowserSession("claude-haiku", mode=permissions.READ_ONLY)
        session.actions = object()
        self.assertNotIn("browser_upload", self.names(session))

    def test_prompt_note_matches_the_model(self):
        self.assertIn("browser_screenshot", browser_tools.prompt_note("claude-haiku"))
        self.assertIn("can't see images", browser_tools.prompt_note("deepseek-chat"))

    def test_urls_get_a_scheme(self):
        self.assertEqual(browser_tools.normalize_url("localhost:5173/cart"), "http://localhost:5173/cart")
        self.assertEqual(browser_tools.normalize_url("example.com"), "https://example.com")
        self.assertEqual(browser_tools.normalize_url("http://x.test/"), "http://x.test/")

    def test_page_tools_need_an_open_page(self):
        result = browser_tools.BrowserSession("deepseek-chat").call("browser_click", {"element": 1})
        self.assertIn("browser_open first", result)


if __name__ == "__main__":
    unittest.main()
