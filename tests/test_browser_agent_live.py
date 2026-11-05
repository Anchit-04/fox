import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
import memory
import paths
from browser import launch
from providers import ProviderResponse, ToolCall

PAGES = {
    "/": """<!doctype html><title>Shop</title>
<style>.hero { width: 1800px } .price { color: #ccc }</style>
<div class=hero>Summer sale</div>
<p id=status>idle</p>
<button onclick="document.getElementById('status').textContent = 'clicked'">Go</button>
<p class=price>Only $12</p>
<label>Password <input type=password></label>""",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path, "")
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


class ScriptedModel:
    def __init__(self, script):
        self.script = list(script)
        self.seen_tools = []
        self.last_turns = []

    def generate(self, history, tools, system_prompt):
        self.seen_tools.append([t["name"] for t in tools])
        self.last_turns = list(history)
        self.system_prompt = system_prompt
        return self.script.pop(0) if self.script else ProviderResponse(text="done", tool_calls=[])


class HumanStandIn:
    def __init__(self):
        self.handoffs = []

    def approve(self, tool_name, args, reason):
        return True

    def start_handoff(self, *, kind, message, url="", task_id=None):
        self.handoffs.append((kind, message, url))
        return f"handoff_{len(self.handoffs)}"

    def handoff_answer(self, request_id):
        return True

    def finish_handoff(self, request_id):
        pass


def step(name, **args):
    return ProviderResponse(text=None, tool_calls=[ToolCall(id=f"call_{name}", name=name, args=args)])


def _installed_browser():
    try:
        return launch.find_browser()
    except launch.BrowserNotFound:
        return None


@unittest.skipIf(_installed_browser() is None, "no Chrome or Brave installed")
class AgentBrowses(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        for patcher in (mock.patch.object(paths, "FOX_HOME", home),
                        mock.patch.object(paths, "_workspace", home),
                        mock.patch.dict(os.environ, {"FOX_BROWSER_HEADLESS": "1"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_script(self, script, model_key="claude-haiku"):
        model = ScriptedModel(script)
        human = HumanStandIn()
        with mock.patch.object(agent, "get_provider", return_value=model):
            agent.run_agent("check the shop page", verbose=False, model_key=model_key, interactive=False,
                            approver=human, mem=memory.Memory.scratch())
        return model, human

    def results(self, model):
        return {r.name: r for turn in model.last_turns for r in turn.tool_results}

    def test_a_seeing_model_browses_checks_layout_and_hands_off_a_password(self):
        from tools import browser_tools
        windowed = mock.patch.object(browser_tools.BrowserSession, "_headless", return_value=False)
        windowed.start()
        self.addCleanup(windowed.stop)
        model, human = self.run_script([
            step("browser_open", url=self.url),
            step("browser_click", element=1),
            step("browser_layout_check"),
            step("browser_screenshot", boxes=True),
            step("browser_type", element=2, text="hunter2"),
        ])
        self.assertEqual(model.seen_tools[0][-1], "browser_open")
        self.assertIn("browser_click", model.seen_tools[1])
        self.assertIn("browser_screenshot", model.seen_tools[1])
        self.assertIn("Browser:", model.system_prompt)
        results = self.results(model)
        self.assertIn('[1] button "Go"', results["browser_open"].content)
        self.assertIn("clicked", results["browser_click"].content)
        self.assertIn("sideways scroll", results["browser_layout_check"].content)
        self.assertIn("low contrast", results["browser_layout_check"].content)
        self.assertEqual(len(results["browser_screenshot"].images), 1)
        self.assertIn("The human pressed Done.", results["browser_type"].content)
        self.assertEqual(human.handoffs[0][0], "login")
        self.assertEqual(human.handoffs[0][2], self.url)
        self.assertIsNone(launch.attach(launch.profile_dir()))

    def test_a_blind_model_never_gets_images(self):
        model, _ = self.run_script([step("browser_open", url=self.url), step("browser_screenshot")],
                                   model_key="deepseek-chat")
        self.assertNotIn("browser_screenshot", model.seen_tools[1])
        self.assertIn("can't see images", model.system_prompt)
        results = self.results(model)
        self.assertEqual(results["browser_screenshot"].images, [])

    def test_an_orchestrator_chat_browses_itself(self):
        from orchestration import orchestrator as orch
        model = ScriptedModel([step("browser_open", url=self.url), step("browser_click", element=1)])
        with mock.patch.object(orch, "get_provider", return_value=model):
            orch.run_orchestrator("open the shop for me", model_key="claude-haiku", mem=memory.Memory.scratch(),
                                  approver=HumanStandIn())
        self.assertEqual(model.seen_tools[0][-1], "browser_open")
        self.assertIn("browser_click", model.seen_tools[1])
        self.assertIn("delegate_task", model.seen_tools[1])
        self.assertIn("Use it yourself when the user wants to see or use a page", model.system_prompt)
        self.assertIn("Browser:", model.system_prompt)
        results = self.results(model)
        self.assertIn('[1] button "Go"', results["browser_open"].content)
        self.assertIn("clicked", results["browser_click"].content)
        self.assertIsNone(launch.attach(launch.profile_dir()))

    def test_show_brings_the_window_forward_unless_headless(self):
        model, _ = self.run_script([step("browser_open", url=self.url, show=True)])
        self.assertIn("headless, so the human can't see", self.results(model)["browser_open"].content)
        from tools import browser_tools
        with mock.patch.object(browser_tools.BrowserSession, "_headless", return_value=False):
            model, _ = self.run_script([step("browser_open", url=self.url, show=True)])
        content = self.results(model)["browser_open"].content
        self.assertTrue(content.startswith("The window is now in front of the human."))
        self.assertIn("Page: Shop", content)


if __name__ == "__main__":
    unittest.main()
