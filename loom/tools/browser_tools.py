import json
import time
from urllib.parse import urlparse

import permissions
from approval import HANDOFF_ENDED
from browser import launch, vision
from browser.actions import ActionError, ActionResult, Actions, NeedsHuman
from browser.cdp import CDPConnection, CDPError, NavigationFailed, Page
from providers import ToolOutput, has_vision

KEEP_SCREENSHOTS = 2
HANDOFF_POLL_SECONDS = 0.4
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")

_hub_owned = False


def hub_owns_browser() -> None:
    global _hub_owned
    _hub_owned = True


def _tool(name: str, description: str, properties: dict | None = None, required: tuple = ()) -> dict:
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}


ELEMENT = {"type": "integer", "description": "The element's number from the latest page text."}

OPEN = _tool("browser_open", "Open a URL in Fox's own browser and get the page back as text with numbered elements.",
             {"url": {"type": "string", "description": "The address, e.g. http://localhost:5173 or https://example.com."},
              "show": {"type": "boolean", "description": "true when the human wants to see the page themselves: "
                                                         "the window comes to the front of their screen."}},
             ("url",))
PAGE_TOOLS = [
    _tool("browser_snapshot", "Read the current page again. Returns only what changed since you last read it, "
                              "unless full is true.", {"full": {"type": "boolean"}}),
    _tool("browser_click", "Click an element by its number.", {"element": ELEMENT}, ("element",)),
    _tool("browser_type", "Replace the text in a text field. submit=true presses Enter afterwards.",
          {"element": ELEMENT, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ("element", "text")),
    _tool("browser_select", "Choose a dropdown option by its visible text or value.",
          {"element": ELEMENT, "option": {"type": "string"}}, ("element", "option")),
    _tool("browser_check", "Tick or untick a checkbox or radio button.",
          {"element": ELEMENT, "checked": {"type": "boolean", "description": "false to untick. Defaults to true."}},
          ("element",)),
    _tool("browser_hover", "Move the mouse over an element, for menus and tooltips.", {"element": ELEMENT},
          ("element",)),
    _tool("browser_press", "Press a key or combination: Enter, Escape, Tab, ArrowDown, ctrl+a and so on.",
          {"keys": {"type": "string"}}, ("keys",)),
    _tool("browser_scroll", "Scroll the page, or bring an element into view.",
          {"direction": {"type": "string", "enum": ["down", "up", "left", "right", "top", "bottom"]},
           "element": ELEMENT}),
    _tool("browser_back", "Go back one page, or forward with forward=true.", {"forward": {"type": "boolean"}}),
    _tool("browser_upload", "Attach project files to a file input, or to the file picker a click just opened "
                            "(then leave element out).",
          {"element": ELEMENT, "files": {"type": "array", "items": {"type": "string"},
                                         "description": "Paths inside the project."}}, ("files",)),
    _tool("browser_dialog", "Answer the confirm or prompt dialog the page is showing.",
          {"accept": {"type": "boolean"}, "text": {"type": "string", "description": "Text to enter in a prompt."}},
          ("accept",)),
    _tool("browser_layout_check", "List layout problems on the current page as text: sideways scrolling, "
                                  "overlaps, covered controls, cut-off text, broken images and low contrast."),
    _tool("browser_ask_human", "Bring the page up in front of the human and wait while they do a step Fox must "
                               "not or cannot do: signing in, card details, codes, CAPTCHAs, or anything you are "
                               "stuck on.",
          {"reason": {"type": "string", "description": "What the human should do, in one sentence."},
           "element": {"type": "integer", "description": "The number of the element the human should start at, "
                                                         "if there is one."}}, ("reason",)),
]
SCREENSHOT = _tool("browser_screenshot", "Take a picture of the visible part of the page. boxes=true draws each "
                                         "element's number on it.", {"boxes": {"type": "boolean"}})
LOOK = _tool("browser_look", "Ask a model that can see a question about how the page looks right now.",
             {"question": {"type": "string"}}, ("question",))

ALL_SCHEMAS = [OPEN, *PAGE_TOOLS, SCREENSHOT, LOOK]
NAMES = frozenset(t["name"] for t in ALL_SCHEMAS)

STATUS_TEXT = {
    "login": "sign in on {host}",
    "payment": "enter your card details on {host}",
    "code": "enter the one-time code on {host}",
    "captcha": "solve the CAPTCHA on {host}",
    "file": "pick a file on {host}",
    "ask": "{reason}",
}
BAR_TEXT = {
    "login": "Sign in here. Fox carries on once you're in.",
    "payment": "Enter your card details here. Fox never reads them.",
    "code": "Enter the one-time code here.",
    "captcha": "Solve the CAPTCHA, then Fox carries on.",
    "file": "Pick the file here, then press Done.",
    "ask": "{reason}",
}
LEADS = {
    "done": "The human pressed Done.",
    "finished": "The human finished the step.",
    "skip": "The human skipped this step; find another way or report it.",
    "ended": "The session ended before the human finished.",
}

BROWSER_NOTE = """
Browser:
- browser_open opens a web page in Fox's own browser. The other browser_* tools appear once a page is open.
  The window stays minimized unless you pass show=true, which you should when the human asks to see a page.
- Pages come back as text with numbered elements, like [8] button "Checkout". Act on them by number. After
  each action you get only what changed.
- Numbers belong to the page they came from; after the page changes, use the new numbers.
- Never type passwords, card details or one-time codes, and never try to solve a CAPTCHA. Call
  browser_ask_human and the human does that step in the window.
"""
SEEING_NOTE = "- browser_screenshot shows you the page; browser_layout_check lists layout problems as text.\n"
BLIND_NOTE = ("- You can't see images. browser_layout_check finds layout problems for you: sideways scrolling, "
              "overlaps, covered buttons, cut-off text, broken images, low contrast.\n")
LOOK_NOTE = "- browser_look asks a model that can see a question about the page.\n"


def used_browser(turns) -> bool:
    return any(call.name in NAMES for turn in turns for call in turn.tool_calls)


def prompt_note(model_key: str) -> str:
    if has_vision(model_key):
        return BROWSER_NOTE + SEEING_NOTE
    return BROWSER_NOTE + BLIND_NOTE + (LOOK_NOTE if vision.helper_model() else "")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ActionError("No URL given.")
    if "://" in url or url.startswith(("about:", "data:", "file:")):
        return url
    host = url.split("/")[0].split(":")[0].lower()
    return ("http://" if host in ("localhost", "127.0.0.1", "0.0.0.0") else "https://") + url


def _element(args: dict) -> int:
    value = args.get("element")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ActionError(f"element must be a number from the page text, not {value!r}.") from None


class BrowserSession:
    def __init__(self, model_key: str, *, mode: str = permissions.WORKSPACE_WRITE, approver=None,
                 task_id: str | None = None, event_sink=None):
        self.model_key = model_key
        self.mode = mode
        self.approver = approver
        self.task_id = task_id
        self.event_sink = event_sink
        self.offer_all = False
        self.browser: launch.Browser | None = None
        self.conn: CDPConnection | None = None
        self.page: Page | None = None
        self.actions: Actions | None = None

    @property
    def active(self) -> bool:
        return self.actions is not None

    def tools(self) -> list[dict]:
        if not self.active and not self.offer_all:
            return [OPEN]
        tools = [OPEN] + [t for t in PAGE_TOOLS
                          if not (t["name"] == "browser_upload" and self.mode == permissions.READ_ONLY)]
        if has_vision(self.model_key):
            tools.append(SCREENSHOT)
        elif vision.helper_model():
            tools.append(LOOK)
        return tools

    def handlers(self) -> dict:
        return {name: (lambda args, name=name: self.call(name, args)) for name in NAMES}

    def call(self, name: str, args: dict) -> str | ToolOutput:
        if name != "browser_open" and not self.active:
            return _error("No page is open. Call browser_open first.")
        try:
            return self._dispatch(name, args or {})
        except NeedsHuman as e:
            return self._handoff(e.need.kind, str(e), e.ref)
        except ActionError as e:
            return _error(str(e))
        except NavigationFailed as e:
            return _error(e.message)
        except (launch.BrowserNotFound, launch.BrowserLaunchFailed) as e:
            return _error(str(e))
        except CDPError as e:
            if self._lost():
                self._drop()
                return _error("The browser window was closed or crashed. browser_open starts a new one.")
            return _error(f"The browser reported an error: {e}")

    def close(self) -> None:
        browser = self.browser
        self._drop()
        if browser is not None and browser.process is not None and not _hub_owned:
            launch.close(browser)
        self.browser = None

    def _dispatch(self, name: str, args: dict) -> str | ToolOutput:
        if name == "browser_open":
            url = normalize_url(args.get("url", ""))
            self._ensure()
            text = self.actions.navigate(url).text
            if args.get("show"):
                if self._headless():
                    return text + "\n\nFox's browser is headless, so the human can't see this window."
                self.actions.hand_to_human()
                return "The window is now in front of the human.\n" + text
            return text
        actions = self.actions
        if name == "browser_snapshot":
            return actions.snapshot(full=bool(args.get("full"))).text
        if name == "browser_click":
            return actions.click(_element(args)).text
        if name == "browser_type":
            return actions.type(_element(args), str(args.get("text", "")), bool(args.get("submit"))).text
        if name == "browser_select":
            return actions.select(_element(args), str(args.get("option", ""))).text
        if name == "browser_check":
            return actions.check(_element(args), args.get("checked", True) is not False).text
        if name == "browser_hover":
            return actions.hover(_element(args)).text
        if name == "browser_press":
            return actions.press(str(args.get("keys", ""))).text
        if name == "browser_scroll":
            ref = _element(args) if args.get("element") is not None else None
            return actions.scroll(str(args.get("direction") or "down"), ref=ref).text
        if name == "browser_back":
            return (actions.forward() if args.get("forward") else actions.back()).text
        if name == "browser_upload":
            files = args.get("files") or []
            ref = _element(args) if args.get("element") is not None else None
            return actions.upload(ref, [files] if isinstance(files, str) else list(files)).text
        if name == "browser_dialog":
            text = args.get("text")
            return actions.answer_dialog(bool(args.get("accept")), None if text is None else str(text)).text
        if actions.dialog:
            return actions.dialog_notice()
        if name == "browser_layout_check":
            return vision.layout_check(self.page, actions.snapshotter)
        if name == "browser_screenshot":
            boxes = bool(args.get("boxes"))
            text = actions.snapshot().text if boxes else "Screenshot of the visible part of the page:"
            if boxes:
                text = "Each box is labelled with its element number.\n\n" + text
            return ToolOutput(text, [vision.screenshot(self.page, actions.snapshotter, boxes=boxes)])
        if name == "browser_look":
            helper = vision.helper_model()
            if helper is None:
                return _error("No seeing model is set up. Use browser_layout_check instead.")
            answer = vision.look(self.page, str(args.get("question", "")), helper,
                                 task_id=self.task_id, event_sink=self.event_sink)
            return f"{helper} looked at the page and says: {answer}"
        if name == "browser_ask_human":
            reason = str(args.get("reason") or "The agent asked for your help on this page.")
            ref = _element(args) if args.get("element") is not None else None
            need = actions._human_check()
            return self._handoff(need.kind if need else "ask", reason, ref)
        return _error(f"Unknown browser tool {name}.")

    def _ensure(self) -> None:
        if self.active:
            return
        self.browser = launch.attach_or_launch()
        self.conn = CDPConnection(self.browser.ws_url)
        self.page = Page(self.conn).open()
        self.actions = Actions(self.page)

    def _url(self) -> str:
        last = self.actions.snapshotter.last if self.actions else None
        return last.url if last else ""

    def _headless(self) -> bool:
        try:
            return self.conn.headless
        except CDPError:
            return False

    def _handoff(self, kind: str, reason: str, ref: int | None = None) -> str:
        url = self._url()
        host = urlparse(url).hostname or url or "the page"
        if self._headless():
            return _error(f"This step needs the human ({reason}), but Fox's browser is headless, so there is no "
                          f"window to show them. Tell the human to do it themselves at {url}.")
        start = getattr(self.approver, "start_handoff", None)
        status = STATUS_TEXT.get(kind, STATUS_TEXT["ask"]).format(host=host, reason=reason)
        request = start(kind=kind, message=status, url=url, task_id=self.task_id) if start else None
        if request is None:
            return _error(f"This step needs the human ({reason}), and nobody can be reached from this run. "
                          "Report what is needed instead.")
        try:
            self.actions.begin_handoff(kind, BAR_TEXT.get(kind, BAR_TEXT["ask"]).format(reason=reason), ref)
            outcome = self._wait_for_human(request)
            result: ActionResult = self.actions.end_handoff(LEADS[outcome])
        except CDPError:
            if self._lost():
                self._drop()
                return _error("The browser window was closed during the handoff. browser_open starts a new one.")
            raise
        finally:
            self.approver.finish_handoff(request)
        return result.text

    def _wait_for_human(self, request: str) -> str:
        while True:
            answer = self.approver.handoff_answer(request)
            if answer == HANDOFF_ENDED:
                return "ended"
            if answer is not None:
                return "done" if answer else "skip"
            outcome = self.actions.poll_handoff()
            if outcome is not None:
                return outcome
            time.sleep(HANDOFF_POLL_SECONDS)

    def _lost(self) -> bool:
        if self.page is None or self.conn is None or self.conn.closed:
            return True
        return not self.page.exists()

    def _drop(self) -> None:
        if self.actions is not None:
            self.actions.close()
        if self.page is not None:
            try:
                self.page.close()
            except CDPError:
                pass
        if self.conn is not None:
            self.conn.close()
        self.actions = self.page = self.conn = None
