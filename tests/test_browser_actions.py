import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import paths
from browser import launch
from browser.actions import ActionError, Actions, NeedsHuman, StaleElement, parse_keys
from browser.cdp import CDPConnection, Page


class Keys(unittest.TestCase):
    def test_named_keys_and_aliases(self):
        self.assertEqual(parse_keys("Enter"), (0, ("Enter", "Enter", 13, "\r")))
        self.assertEqual(parse_keys("esc")[1][0], "Escape")
        self.assertEqual(parse_keys("down")[1][0], "ArrowDown")

    def test_modifiers_drop_the_typed_text(self):
        modifiers, key = parse_keys("ctrl+shift+k")
        self.assertEqual(modifiers, 2 | 8)
        self.assertEqual(key, ("K", "KeyK", 75, ""))

    def test_shift_alone_keeps_the_capital(self):
        self.assertEqual(parse_keys("shift+a"), (8, ("A", "KeyA", 65, "A")))

    def test_digits_and_bad_input(self):
        self.assertEqual(parse_keys("7")[1][:2], ("7", "Digit7"))
        with self.assertRaises(ActionError):
            parse_keys("hyper+x")
        with self.assertRaises(ActionError):
            parse_keys("notakey")


PAGES = {
    "/": """<!doctype html><title>One</title>
<p id=status>idle</p>
<button id=go onclick="document.getElementById('status').textContent = 'clicked'">Go</button>
<a href="/two">Next page</a>
<a href="/two" target="_blank">Next in a new tab</a>
<form id=f onsubmit="event.preventDefault(); document.getElementById('status').textContent = 'sent ' + this.q.value">
  <label>Search <input name=q oninput="document.getElementById('mirror').textContent = this.value"></label>
  <label>Password <input type=password name=pw></label>
  <button>Find</button>
</form>
<p id=mirror></p>
<select aria-label="Size" onchange="document.getElementById('status').textContent = 'size ' + this.value">
  <option value=s>Small</option><option value=m>Medium</option><option value=l disabled>Large</option>
</select>
<label><input type=checkbox id=agree> I agree</label>
<button disabled>Locked</button>
<div style="position:relative">
  <button id=under onclick="document.getElementById('status').textContent = 'under'">Under</button>
  <div id=veil style="position:absolute; inset:0; background:white">Cookie banner</div>
</div>
<button onclick="alert('Saved!')">Save</button>
<button onclick="document.getElementById('status').textContent = confirm('Delete it?') ? 'deleted' : 'kept'">Delete</button>
<button onclick="document.getElementById('status').textContent = 'hi ' + prompt('Name?')">Name</button>
<input type=file id=visible-file aria-label="Attachment">
<input type=file id=hidden-file style="display:none" onchange="document.getElementById('status').textContent = 'picked ' + this.files[0].name">
<button onclick="document.getElementById('hidden-file').click()">Upload photo</button>
<p id=keys></p>
<script>
  addEventListener("keydown", e => {
    if (["Control", "Shift", "Alt", "Meta"].includes(e.key)) return;
    document.getElementById("keys").textContent = (e.ctrlKey ? "ctrl+" : "") + (e.shiftKey ? "shift+" : "") + e.key;
  });
</script>
<div style="height:3000px"></div>
<p>Bottom of the page</p>""",
    "/two": "<!doctype html><title>Two</title><h1>Second page</h1>",
    "/login": "<!doctype html><title>Sign in</title><div style='height:1500px'>Welcome back</div>"
              "<form action=/two><label>Email <input name=email></label>"
              "<label>Password <input type=password name=pw></label><button>Sign in</button></form>",
    "/pay": "<!doctype html><title>Pay</title><label>Card number <input autocomplete=cc-number "
            "value=4242424242424242></label><label>Name on card <input autocomplete=cc-name value=Ann></label>",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0])
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, *args):
        pass


def _installed_browser():
    try:
        return launch.find_browser()
    except launch.BrowserNotFound:
        return None


@unittest.skipIf(_installed_browser() is None, "no Chrome or Brave installed")
class LiveActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        profile = root / "profile"
        profile.mkdir()
        cls.workspace = root / "project"
        cls.workspace.mkdir()
        (cls.workspace / "photo.png").write_bytes(b"\x89PNG fake")
        (root / "secret.txt").write_text("keep out", encoding="utf-8")
        cls.outside = root / "secret.txt"
        cls.browser = launch.attach_or_launch(profile=profile, headless=True)
        cls.conn = CDPConnection(cls.browser.ws_url)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        launch.close(cls.browser)
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        patcher = mock.patch.object(paths, "_workspace", self.workspace)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.page = Page(self.conn).open()
        self.addCleanup(self.page.close)
        self.actions = Actions(self.page)
        self.addCleanup(self.actions.close)
        self.first = self.actions.navigate(self.base + "/")

    def ref(self, text):
        for node in self.actions.snapshotter.last.nodes:
            if node.ref is not None and text in node.line():
                return node.ref
        raise AssertionError(f"no numbered element matching {text!r}")

    def status(self):
        return self.page.evaluate("document.getElementById('status').textContent")

    def test_navigate_returns_the_page(self):
        self.assertIn("Opened", self.first.text)
        self.assertIn("Page: One", self.first.text)
        self.assertIn('button "Go"', self.first.text)

    def test_click_shows_what_changed(self):
        result = self.actions.click(self.ref('button "Go"'))
        self.assertEqual(self.status(), "clicked")
        self.assertIn('Clicked [', result.text)
        self.assertIn("Changes since the last snapshot", result.text)
        self.assertIn("clicked", result.text)

    def test_click_a_link_follows_it(self):
        result = self.actions.click(self.ref('link "Next page"'))
        self.assertIn("Page: Two", result.text)
        self.assertIn("Second page", result.text)

    def test_a_new_tab_link_opens_here(self):
        result = self.actions.click(self.ref('link "Next in a new tab"'))
        self.assertIn("in this tab instead", result.text)
        self.assertIn("Page: Two", result.text)
        pages = [t for t in self.conn.send("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        self.assertFalse(any(t.get("openerId") == self.page.target_id for t in pages))

    def test_type_fires_input_events_and_submit(self):
        result = self.actions.type(self.ref('textbox "Search"'), "blue mug", submit=True)
        self.assertEqual(self.page.evaluate("document.getElementById('mirror').textContent"), "blue mug")
        self.assertEqual(self.status(), "sent blue mug")
        self.assertIn('value="blue mug"', result.text)

    def test_type_replaces_what_was_there(self):
        box = self.ref('textbox "Search"')
        self.actions.type(box, "first")
        self.actions.type(box, "second")
        self.assertEqual(self.page.evaluate("document.querySelector('[name=q]').value"), "second")

    def test_passwords_go_to_the_human(self):
        with self.assertRaises(NeedsHuman) as caught:
            self.actions.type(self.ref('textbox "Password"'), "hunter2")
        self.assertEqual(caught.exception.need.kind, "login")
        self.assertEqual(self.page.evaluate("document.querySelector('[name=pw]').value"), "")

    def test_a_password_page_is_flagged(self):
        result = self.actions.navigate(self.base + "/login")
        self.assertEqual(result.human.kind, "login")
        self.assertIn("hand the page to the human", result.text)

    def test_select_by_text_or_value(self):
        size = self.ref('combobox "Size"')
        self.actions.select(size, "medium")
        self.assertEqual(self.status(), "size m")
        self.actions.select(size, "s")
        self.assertEqual(self.status(), "size s")

    def test_select_reports_the_choices(self):
        size = self.ref('combobox "Size"')
        with self.assertRaises(ActionError) as caught:
            self.actions.select(size, "Huge")
        self.assertIn("Medium", str(caught.exception))
        with self.assertRaises(ActionError):
            self.actions.select(size, "Large")

    def test_check_is_idempotent(self):
        box = self.ref('checkbox "I agree"')
        self.actions.check(box)
        self.assertTrue(self.page.evaluate("document.getElementById('agree').checked"))
        self.assertIn("already ticked", self.actions.check(box).text)
        self.actions.check(box, on=False)
        self.assertFalse(self.page.evaluate("document.getElementById('agree').checked"))

    def test_disabled_and_covered_buttons_are_refused(self):
        with self.assertRaises(ActionError) as caught:
            self.actions.click(self.ref('button "Locked"'))
        self.assertIn("disabled", str(caught.exception))
        with self.assertRaises(ActionError) as caught:
            self.actions.click(self.ref('button "Under"'))
        self.assertIn("Cookie banner", str(caught.exception))
        self.assertEqual(self.status(), "idle")

    def test_alerts_are_accepted_and_reported(self):
        result = self.actions.click(self.ref('button "Save"'))
        self.assertIn('"Saved!"', result.text)
        self.assertIsNone(self.actions.dialog)
        self.assertEqual(self.page.evaluate("1 + 1"), 2)

    def test_confirm_waits_for_an_answer(self):
        result = self.actions.click(self.ref('button "Delete"'))
        self.assertIn('confirm dialog: "Delete it?"', result.text)
        with self.assertRaises(ActionError):
            self.actions.click(self.ref('button "Go"'))
        self.assertIn("confirm dialog", self.actions.snapshot().text)
        self.actions.answer_dialog(False)
        self.assertEqual(self.status(), "kept")
        self.actions.click(self.ref('button "Delete"'))
        self.actions.answer_dialog(True)
        self.assertEqual(self.status(), "deleted")

    def test_prompt_takes_text(self):
        self.actions.click(self.ref('button "Name"'))
        result = self.actions.answer_dialog(True, "Ann")
        self.assertEqual(self.status(), "hi Ann")
        self.assertIn('with "Ann"', result.text)

    def test_upload_to_a_file_input(self):
        result = self.actions.upload(self.ref('file "Attachment"'), "photo.png")
        self.assertEqual(self.page.evaluate("document.getElementById('visible-file').files[0].name"), "photo.png")
        self.assertIn("Attached photo.png", result.text)

    def test_upload_through_the_file_picker(self):
        result = self.actions.click(self.ref('button "Upload photo"'))
        self.assertIn("A file picker opened", result.text)
        self.actions.upload(None, "photo.png")
        self.assertEqual(self.status(), "picked photo.png")

    def test_files_outside_the_project_go_to_the_human(self):
        with self.assertRaises(NeedsHuman) as caught:
            self.actions.upload(self.ref('file "Attachment"'), str(self.outside))
        self.assertEqual(caught.exception.need.kind, "file")
        with self.assertRaises(ActionError):
            self.actions.upload(self.ref('file "Attachment"'), "missing.png")

    def test_old_numbers_fail_after_navigation(self):
        go = self.ref('button "Go"')
        self.actions.navigate(self.base + "/two")
        with self.assertRaises(StaleElement):
            self.actions.click(go)

    def test_keys_reach_the_page(self):
        self.actions.press("ctrl+shift+k")
        self.assertEqual(self.page.evaluate("document.getElementById('keys').textContent"), "ctrl+shift+K")
        self.actions.press("Escape")
        self.assertEqual(self.page.evaluate("document.getElementById('keys').textContent"), "Escape")

    def test_scroll_moves_the_page(self):
        result = self.actions.scroll("down")
        self.assertGreater(self.page.evaluate("scrollY"), 0)
        self.assertIn("Scrolled to", result.text)
        self.actions.scroll("bottom")
        self.assertIn("Bottom of the page", self.actions.snapshot(full=True).text)

    def test_back_and_forward(self):
        self.actions.navigate(self.base + "/two")
        self.assertIn("Page: One", self.actions.back().text)
        self.assertIn("Page: Two", self.actions.forward().text)
        with self.assertRaises(ActionError):
            self.actions.forward()

    def test_only_web_and_project_urls_open(self):
        with self.assertRaises(ActionError):
            self.actions.navigate("chrome://settings")
        with self.assertRaises(ActionError) as caught:
            self.actions.navigate(self.outside.as_uri())
        self.assertNotIsInstance(caught.exception, NeedsHuman)

    def start_login_handoff(self):
        self.actions.navigate(self.base + "/login")
        password = self.ref('textbox "Password"')
        self.actions.begin_handoff("login", "Sign in here.", password)
        self.addCleanup(lambda: self.actions.handoff and self.actions.end_handoff("cleanup"))

    def test_the_handoff_opens_at_the_field_with_the_fox_bar(self):
        self.start_login_handoff()
        self.assertTrue(self.page.evaluate("!!document.getElementById('__fox_handoff')"))
        self.assertEqual(self.page.evaluate("document.activeElement.name"), "pw")
        self.assertGreater(self.page.evaluate("scrollY"), 0)
        self.assertIsNone(self.actions.poll_handoff())

    def test_finishing_the_login_ends_the_handoff_by_itself(self):
        self.start_login_handoff()
        self.page.evaluate("document.querySelector('[name=pw]').value = 'secret'; document.forms[0].submit()")
        outcome = None
        for _ in range(20):
            outcome = self.actions.poll_handoff()
            if outcome:
                break
            time.sleep(0.2)
        self.assertEqual(outcome, "finished")
        result = self.actions.end_handoff("The human finished the step.")
        self.assertIn("The human finished the step.", result.text)
        self.assertIn("Page: Two", result.text)
        self.assertFalse(self.page.evaluate("!!document.getElementById('__fox_handoff')"))

    def test_a_failed_login_keeps_waiting_and_brings_the_bar_back(self):
        self.start_login_handoff()
        self.page.evaluate("location.reload()")
        time.sleep(1)
        self.assertIsNone(self.actions.poll_handoff())
        self.assertIsNone(self.actions.poll_handoff())
        self.assertTrue(self.page.evaluate("!!document.getElementById('__fox_handoff')"))

    def test_clicking_done_in_the_bar(self):
        self.start_login_handoff()
        box = self.page.evaluate(
            "(() => { const r = globalThis.__foxHandoff.root.querySelector('.done').getBoundingClientRect();"
            " return [r.left + r.width / 2, r.top + r.height / 2]; })()",
            context_id=self.actions.snapshotter.world)
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.page.call("Input.dispatchMouseEvent", {"type": kind, "x": box[0], "y": box[1], "button": "left",
                                                         "buttons": buttons, "clickCount": 1})
        self.assertEqual(self.actions.poll_handoff(), "done")
        self.assertEqual(self.page.evaluate("document.querySelector('[name=pw]').value"), "")

    def test_card_numbers_never_reach_the_snapshot(self):
        result = self.actions.navigate(self.base + "/pay")
        self.assertNotIn("4242", result.text)
        self.assertIn('value="********"', result.text)


if __name__ == "__main__":
    unittest.main()
