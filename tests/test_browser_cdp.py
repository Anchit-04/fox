import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from websockets.sync.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from browser import launch
from browser.cdp import (
    CDPClosed,
    CDPConnection,
    CDPError,
    CDPTimeout,
    JavaScriptError,
    NavigationFailed,
    Page,
)


class FakeBrowser:
    def __init__(self):
        self.received = []
        self.server = serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/devtools/browser/fake"

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)

    def _handle(self, ws):
        held = []
        for raw in ws:
            message = json.loads(raw)
            self.received.append(message)
            method, message_id = message["method"], message["id"]
            if method == "Echo":
                ws.send(json.dumps({"id": message_id, "result": message["params"]}))
            elif method == "Fail":
                ws.send(json.dumps({"id": message_id, "error": {"code": -32000, "message": "nope"}}))
            elif method == "Hold":
                held.append(message_id)
            elif method == "Release":
                ws.send(json.dumps({"id": message_id, "result": {"order": "release"}}))
                for old in held:
                    ws.send(json.dumps({"id": old, "result": {"order": "held"}}))
                held.clear()
            elif method == "Emit":
                for session in (None, "other", message.get("sessionId")):
                    event = {"method": "Thing.happened", "params": {"for": session}}
                    if session:
                        event["sessionId"] = session
                    ws.send(json.dumps(event))
                ws.send(json.dumps({"id": message_id, "result": {}}))
            elif method == "Hangup":
                ws.close()
                return


class ConnectionAgainstAFake(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBrowser()
        self.addCleanup(self.fake.stop)
        self.conn = CDPConnection(self.fake.url)
        self.addCleanup(self.conn.close)

    def test_a_reply_comes_back_to_its_caller(self):
        self.assertEqual(self.conn.send("Echo", {"x": 1}), {"x": 1})

    def test_session_id_is_sent_only_when_given(self):
        self.conn.send("Echo")
        self.conn.send("Echo", session_id="tab-1")
        self.assertNotIn("sessionId", self.fake.received[0])
        self.assertEqual(self.fake.received[1]["sessionId"], "tab-1")

    def test_replies_out_of_order_reach_the_right_thread(self):
        result = {}
        holder = threading.Thread(target=lambda: result.setdefault("held", self.conn.send("Hold")))
        holder.start()
        while not any(m["method"] == "Hold" for m in self.fake.received):
            pass
        self.assertEqual(self.conn.send("Release"), {"order": "release"})
        holder.join(timeout=5)
        self.assertEqual(result["held"], {"order": "held"})

    def test_a_protocol_error_raises_with_its_code(self):
        with self.assertRaises(CDPError) as caught:
            self.conn.send("Fail")
        self.assertEqual(caught.exception.code, -32000)
        self.assertIn("nope", str(caught.exception))

    def test_no_reply_times_out(self):
        with self.assertRaises(CDPTimeout):
            self.conn.send("Hold", timeout=0.2)

    def test_events_reach_only_the_matching_session(self):
        with self.conn.expect("Thing.happened", "tab-1") as waiter:
            self.conn.send("Emit", session_id="tab-1")
            self.assertEqual(waiter.wait(1), {"for": "tab-1"})
            with self.assertRaises(CDPTimeout):
                waiter.wait(0.2)

    def test_where_skips_events_that_do_not_fit(self):
        with self.conn.expect("Thing.happened") as waiter:
            self.conn.send("Emit")
            self.assertEqual(waiter.wait(1, where=lambda e: e["for"] is None), {"for": None})

    def test_a_cancelled_waiter_gets_nothing(self):
        waiter = self.conn.expect("Thing.happened")
        waiter.cancel()
        self.conn.send("Emit")
        with self.assertRaises(CDPTimeout):
            waiter.wait(0.2)

    def test_hangup_fails_the_waiting_caller_and_later_sends(self):
        with self.conn.expect("Thing.happened") as waiter:
            with self.assertRaises(CDPClosed):
                self.conn.send("Hangup", timeout=5)
            with self.assertRaises(CDPClosed):
                waiter.wait(1)
        with self.assertRaises(CDPClosed):
            self.conn.send("Echo")
        self.assertTrue(self.conn.closed)

    def test_connecting_to_nothing_raises_closed(self):
        with self.assertRaises(CDPClosed):
            CDPConnection("ws://127.0.0.1:1/devtools/browser/none", open_timeout=1)


def _installed_browser():
    try:
        return launch.find_browser()
    except launch.BrowserNotFound:
        return None


def _data_url(html):
    return "data:text/html," + urllib.parse.quote(html)


@unittest.skipIf(_installed_browser() is None, "no Chrome or Brave installed")
class LivePages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        profile = Path(cls.tmp.name) / "default"
        profile.mkdir()
        cls.browser = launch.attach_or_launch(profile=profile, headless=True)
        cls.conn = CDPConnection(cls.browser.ws_url)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        launch.close(cls.browser)
        cls.tmp.cleanup()

    def _tabs(self):
        targets = self.conn.send("Target.getTargets")["targetInfos"]
        return {t["targetId"] for t in targets if t["type"] == "page"}

    def test_navigate_then_evaluate(self):
        with Page(self.conn).open() as page:
            page.navigate(_data_url("<title>fox</title><h1>hi</h1>"))
            self.assertEqual(page.evaluate("document.title"), "fox")
            self.assertEqual(page.evaluate("document.querySelector('h1').textContent"), "hi")

    def test_evaluate_awaits_promises_and_returns_values(self):
        with Page(self.conn).open() as page:
            self.assertEqual(page.evaluate("Promise.resolve({a: [1, 2]})"), {"a": [1, 2]})

    def test_a_thrown_error_raises(self):
        with Page(self.conn).open() as page:
            with self.assertRaises(JavaScriptError) as caught:
                page.evaluate("throw new Error('boom')")
            self.assertIn("boom", str(caught.exception))

    def test_an_unreachable_site_raises(self):
        with Page(self.conn).open() as page:
            with self.assertRaises(NavigationFailed) as caught:
                page.navigate("http://127.0.0.1:1/")
            self.assertIn("ERR_", str(caught.exception))

    def test_a_hash_change_does_not_wait_for_a_load(self):
        with Page(self.conn).open() as page:
            page.navigate(_data_url("<p id='x'>x</p>"))
            page.navigate(_data_url("<p id='x'>x</p>") + "#x")

    def test_tabs_are_separate_and_closed_tabs_are_gone(self):
        first = Page(self.conn).open()
        second = Page(self.conn).open()
        try:
            first.navigate(_data_url("<title>one</title>"))
            second.navigate(_data_url("<title>two</title>"))
            self.assertEqual(first.evaluate("document.title"), "one")
            self.assertEqual(second.evaluate("document.title"), "two")
            self.assertTrue({first.target_id, second.target_id} <= self._tabs())
            closed = first.target_id
        finally:
            first.close()
            second.close()
        self.assertNotIn(closed, self._tabs())

    def test_two_connections_share_one_browser(self):
        with CDPConnection(self.browser.ws_url) as other, Page(other).open() as page:
            self.assertIn(page.target_id, self._tabs())


@unittest.skipIf(_installed_browser() is None, "no Chrome or Brave installed")
class LiveClose(unittest.TestCase):
    def test_close_shuts_a_browser_someone_else_launched(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "default"
            profile.mkdir()
            ours = launch.attach_or_launch(profile=profile, headless=True)
            try:
                attached = launch.attach(profile)
                launch.close(attached)
                ours.process.wait(timeout=5)
                self.assertIsNone(launch.attach(profile))
            finally:
                launch.stop(ours)


if __name__ == "__main__":
    unittest.main()
