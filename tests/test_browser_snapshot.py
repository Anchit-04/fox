import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from browser import launch
from browser.cdp import CDPConnection, CDPError, Page
from browser.snapshot import Node, Snapshot, Snapshotter, diff, render


def snap(nodes, doc="d1", url="http://shop.test/", scroll=(0, 800)):
    return Snapshot(url=url, title="Shop", doc=doc, viewport=(1280, 800), scroll=scroll, nodes=nodes)


def button(ref, name, in_view=True, **props):
    return Node(role="button", name=name, depth=1, in_view=in_view, ref=ref, props=tuple(props.items()))


def text(name, in_view=True, depth=1):
    return Node(role="text", name=name, depth=depth, in_view=in_view)


class Lines(unittest.TestCase):
    def test_numbered_element_with_props(self):
        node = Node(role="textbox", name="Email", depth=2, in_view=True, ref=4,
                    props=(("value", ""), ("placeholder", "you@x.com"), ("required", True)))
        self.assertEqual(node.line(), '    [4] textbox "Email" value="" placeholder="you@x.com" required')

    def test_false_flags_read_naturally(self):
        self.assertEqual(button(1, "Go", checked=False).line(), '  [1] button "Go" unchecked')
        self.assertEqual(button(1, "Menu", expanded=False).line(), '  [1] button "Menu" not expanded')

    def test_text_is_plain_and_names_are_escaped(self):
        self.assertEqual(text("Total: $42").line(), "  Total: $42")
        self.assertEqual(button(2, 'Say "hi"').line(), '  [2] button "Say \\"hi\\""')

    def test_indent_is_capped(self):
        self.assertEqual(text("deep", depth=30).line(), "  " * 8 + "deep")


class Render(unittest.TestCase):
    def test_small_page_is_shown_whole(self):
        output, kept = render(snap([button(1, "Buy"), text("Total")]))
        self.assertIn('[1] button "Buy"', output)
        self.assertIn("Total", output)
        self.assertNotIn("skipped", output)
        self.assertEqual(kept, {0, 1})

    def test_budget_keeps_the_viewport_then_numbered_elements(self):
        nodes = [button(1, "Top")] + [text(f"paragraph {i} " + "x" * 50, in_view=False) for i in range(40)]
        nodes.append(button(2, "Far below", in_view=False))
        output, kept = render(snap(nodes), budget=200)
        self.assertIn('[1] button "Top"', output)
        self.assertIn('[2] button "Far below"', output)
        self.assertLessEqual(len(output.split("\n\n")[1]), 200)
        self.assertIn("of 42 lines (2 of 2 numbered elements)", output)

    def test_skipped_runs_are_marked_where_they_happen(self):
        nodes = [text("a"), text("b" * 80, in_view=False), text("c")]
        output, _ = render(snap(nodes), budget=10)
        body = output.split("\n\n")[1].splitlines()
        self.assertEqual(body, ["  a", "…", "  c"])

    def test_long_urls_are_clipped(self):
        output, _ = render(snap([], url="data:text/html," + "x" * 5000))
        self.assertLess(len(output), 400)
        self.assertIn("no visible content", output)


class Diff(unittest.TestCase):
    def test_another_document_has_no_diff(self):
        self.assertIsNone(diff(snap([]), snap([], doc="d2")))

    def test_added_removed_and_changed(self):
        old = snap([button(1, "Home"), text("Cart is empty"), button(2, "Remember", checked=True)])
        new = snap([button(1, "Home"), button(3, "Remove"), button(2, "Remember", checked=False)])
        self.assertEqual(diff(old, new), [
            'after [1] button "Home":',
            "- Cart is empty",
            '+ [3] button "Remove"',
            '~ [2] button "Remember" unchecked',
        ])

    def test_url_title_and_scroll_changes_are_reported(self):
        old = snap([])
        new = Snapshot(url="http://shop.test/#x", title="Cart", doc="d1", viewport=(1280, 800),
                       scroll=(400, 800), nodes=[])
        self.assertEqual(diff(old, new), ["URL: http://shop.test/#x", "Title: Cart", "Scrolled to 400px of 800px"])

    def test_no_changes_is_empty(self):
        self.assertEqual(diff(snap([button(1, "A")]), snap([button(1, "A")])), [])


class FakePage:
    def __init__(self, results):
        self.results = list(results)
        self.worlds = 0

    def create_world(self):
        self.worlds += 1
        return self.worlds

    def evaluate(self, expression, context_id=None):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def raw(nodes, doc="d1", scroll=(0, 800)):
    return {"url": "http://shop.test/", "title": "Shop", "doc": doc, "viewport": [1280, 800],
            "scroll": list(scroll), "nodes": nodes}


def raw_button(ref, name, in_view=True):
    return {"role": "button", "name": name, "depth": 0, "inView": in_view, "ref": ref, "props": {}}


class Updates(unittest.TestCase):
    def test_first_call_is_a_full_snapshot_then_diffs(self):
        many = [raw_button(i, f"Item number {i}") for i in range(1, 30)]
        page = FakePage([raw(many), raw(many + [raw_button(99, "New")]), raw(many + [raw_button(99, "New")])])
        snapshotter = Snapshotter(page)
        self.assertIn("Page: Shop", snapshotter.update())
        self.assertIn('+ [99] button "New"', snapshotter.update())
        self.assertEqual(snapshotter.update(), "No changes since the last snapshot.")

    def test_a_new_document_gets_a_full_snapshot(self):
        page = FakePage([raw([raw_button(1, "A")]), raw([raw_button(1, "B")], doc="d2")])
        snapshotter = Snapshotter(page)
        snapshotter.update()
        self.assertIn("Page: Shop", snapshotter.update())

    def test_a_big_change_falls_back_to_the_full_snapshot(self):
        before = [raw_button(i, f"Old {i}") for i in range(1, 10)]
        after = [raw_button(i, f"New {i}") for i in range(10, 20)]
        snapshotter = Snapshotter(FakePage([raw(before), raw(after)]))
        snapshotter.update()
        output = snapshotter.update()
        self.assertTrue(output.startswith("Page: Shop"))
        self.assertIn('[10] button "New 10"', output)

    def test_scrolling_shows_what_is_newly_in_view(self):
        nodes = [raw_button(1, "Top")] + [raw_button(i, f"Below {i} " + "y" * 40, in_view=False) for i in range(2, 40)]
        scrolled = [dict(n, inView=n["ref"] in (20, 21)) for n in nodes]
        snapshotter = Snapshotter(FakePage([raw(nodes), raw(scrolled, scroll=(900, 3000))]), budget=400)
        snapshotter.update()
        output = snapshotter.update()
        self.assertIn("Scrolled to 900px of 3000px", output)
        self.assertIn("now in view:", output)
        self.assertIn('[20] button "Below 20', output)
        self.assertNotIn("[2] button", output)

    def test_a_lost_world_is_recreated_once(self):
        page = FakePage([CDPError("Cannot find context with specified id", -32000), raw([raw_button(1, "A")])])
        snapshotter = Snapshotter(page)
        self.assertIn("[1] button", snapshotter.full())
        self.assertEqual(page.worlds, 2)

    def test_other_errors_are_not_retried(self):
        page = FakePage([CDPError("Target closed", -32000), raw([])])
        with self.assertRaises(CDPError):
            Snapshotter(page).full()


def _installed_browser():
    try:
        return launch.find_browser()
    except launch.BrowserNotFound:
        return None


def _data_url(html):
    return "data:text/html," + urllib.parse.quote(html)


SHOP = """<title>Shop</title>
<header><nav><a href="/">Home</a> <a href="/cart">Cart</a></nav></header>
<main>
  <h1>Your cart</h1>
  <ul><li>Blue mug <b>$12</b></li><li><a href="/p/2">Red plate</a></li></ul>
  <form>
    <label>Email <input type=email placeholder="you@x.com" required></label>
    <label><input type=checkbox checked> Remember me</label>
    <input type=password aria-label="Password" value="hunter2">
    <select aria-label="Shipping"><option>Standard</option><option selected>Express</option></select>
    <button type=submit>Checkout</button>
    <div style="cursor:pointer">Apply <span>coupon</span></div>
    <div style="display:none"><button>Hidden button</button></div>
    <div style="visibility:hidden">Invisible words</div>
    <p>Total: <b>$42.00</b> incl. tax</p>
  </form>
  <h2><a href="/help">Need help?</a></h2>
  <div id=host></div>
</main>
<script>
  const root = document.getElementById("host").attachShadow({mode: "open"});
  root.innerHTML = "<button>Inside shadow</button>";
</script>"""


@unittest.skipIf(_installed_browser() is None, "no Chrome or Brave installed")
class LiveSnapshots(unittest.TestCase):
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

    def setUp(self):
        self.page = Page(self.conn).open()
        self.addCleanup(self.page.close)
        self.page.navigate(_data_url(SHOP))
        self.snapshotter = Snapshotter(self.page)

    def lines(self):
        return [node.line().strip() for node in self.snapshotter.take().nodes]

    def test_the_page_reads_as_expected(self):
        lines = self.lines()
        for expected in [
            '[1] link "Home"',
            'heading "Your cart" level=1',
            'listitem "Blue mug $12"',
            '[4] textbox "Email" value="" placeholder="you@x.com" required',
            '[5] checkbox "Remember me" checked',
            '[7] combobox "Shipping" value="Express" options=2',
            '[8] button "Checkout"',
            '[9] clickable "Apply coupon"',
            "Total: $42.00 incl. tax",
            "heading level=2",
            '[10] link "Need help?"',
            '[11] button "Inside shadow"',
        ]:
            self.assertIn(expected, lines)

    def test_passwords_are_masked(self):
        self.assertIn('[6] textbox "Password" value="*******"', self.lines())

    def test_hidden_things_are_left_out(self):
        joined = "\n".join(self.lines())
        self.assertNotIn("Hidden button", joined)
        self.assertNotIn("Invisible words", joined)

    def test_label_text_is_not_repeated(self):
        self.assertEqual(sum("Remember me" in line for line in self.lines()), 1)

    def test_numbers_survive_changes_to_the_page(self):
        self.snapshotter.full()
        self.page.evaluate("document.querySelector('ul').insertAdjacentHTML('afterbegin', '<li><button>Undo</button></li>')")
        output = self.snapshotter.update()
        self.assertIn('+ [12] button "Undo"', output)
        self.assertIn('[8] button "Checkout"', self.lines())

    def test_typing_shows_up_as_a_change(self):
        self.snapshotter.full()
        self.page.evaluate("document.querySelector('input[type=email]').value = 'a@b.co'")
        self.assertIn('~ [4] textbox "Email" value="a@b.co" required', self.snapshotter.update())

    def test_navigating_starts_the_numbers_again(self):
        self.snapshotter.full()
        self.page.navigate(_data_url("<title>Two</title><button>Only</button>"))
        output = self.snapshotter.update()
        self.assertIn("Page: Two", output)
        self.assertIn('[1] button "Only"', output)

    def test_the_page_cannot_see_fox(self):
        self.snapshotter.full()
        self.assertIsNone(self.page.evaluate("window.__fox"))

    def test_a_long_page_stays_within_budget(self):
        rows = "".join(f"<p>Row {i} with some filler text to make it longer</p><button>Act {i}</button>" for i in range(400))
        self.page.navigate(_data_url(f"<title>Long</title>{rows}"))
        output = self.snapshotter.full(budget=3000)
        self.assertLess(len(output), 3600)
        self.assertIn('button "Act 0"', output)
        self.assertIn("\n…\n", output)


if __name__ == "__main__":
    unittest.main()
