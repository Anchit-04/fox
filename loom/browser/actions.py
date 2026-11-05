import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import paths
from browser.cdp import CDPAborted, CDPError, CDPTimeout, Page
from browser.snapshot import Snapshotter

INPUT_TIMEOUT = 10.0
NAVIGATION_GRACE = 0.25
SETTLE_TIMEOUT = 5.0
QUIET_MS = 250
ALLOWED_SCHEMES = ("http", "https", "about", "data")

KEYS = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, ""),
    "escape": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "delete": ("Delete", "Delete", 46, ""),
    "space": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "arrowright": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""),
    "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""),
    "pagedown": ("PageDown", "PageDown", 34, ""),
}
KEY_ALIASES = {"return": "enter", "esc": "escape", "up": "arrowup", "down": "arrowdown",
               "left": "arrowleft", "right": "arrowright", "del": "delete"}
MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "win": 4, "shift": 8}
MODIFIER_KEYS = {1: ("Alt", "AltLeft", 18), 2: ("Control", "ControlLeft", 17),
                 4: ("Meta", "MetaLeft", 91), 8: ("Shift", "ShiftLeft", 16)}

PREPARE_JS = r"""
function () {
  if (!this.isConnected) return { gone: true };
  this.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
  const r = this.getBoundingClientRect();
  const tag = this.tagName;
  const type = (this.getAttribute("type") || "").toLowerCase();
  const auto = (this.getAttribute("autocomplete") || "").toLowerCase();
  const hint = [this.name, this.id, this.getAttribute("aria-label"), this.placeholder].join(" ").toLowerCase();
  const NOT_TEXT = ["checkbox", "radio", "button", "submit", "reset", "file", "image", "range", "color", "hidden"];
  const info = {
    tag, type,
    x: r.left + r.width / 2, y: r.top + r.height / 2, width: r.width, height: r.height,
    disabled: !!this.disabled || this.getAttribute("aria-disabled") === "true",
    editable: tag === "TEXTAREA" || (tag === "INPUT" && !NOT_TEXT.includes(type)) || this.isContentEditable,
    password: tag === "INPUT" && type === "password",
    payment: auto.startsWith("cc-") || /card.?(number|no)|cvc|cvv|security.?code/.test(hint),
    code: auto === "one-time-code",
    checked: tag === "INPUT" ? this.checked : this.getAttribute("aria-checked") === "true",
    multiple: !!this.multiple,
    covered: null,
  };
  if (r.width > 0 && r.height > 0 && info.x >= 0 && info.y >= 0 && info.x < innerWidth && info.y < innerHeight) {
    const root = this.getRootNode();
    const hit = (root.elementFromPoint ? root : document).elementFromPoint(info.x, info.y);
    const label = hit && hit.closest ? hit.closest("label") : null;
    const fine = !hit || hit === this || this.contains(hit) || hit.contains(this) || (label && label.control === this);
    if (!fine) {
      const text = (hit.innerText || "").replace(/\s+/g, " ").trim().slice(0, 80);
      info.covered = hit.tagName.toLowerCase() + (hit.id ? "#" + hit.id : "") + (text ? ` "${text}"` : "");
    }
  }
  return info;
}
"""

CLEAR_JS = r"""
function () {
  this.focus();
  if (this.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(this);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return;
  }
  const proto = this instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(this, "");
  this.dispatchEvent(new Event("input", { bubbles: true }));
}
"""

SELECT_JS = r"""
function (wanted) {
  if (this.tagName !== "SELECT") return { error: "not-select" };
  const norm = s => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const target = norm(wanted);
  const options = [...this.options];
  const match = options.find(o => norm(o.text) === target)
    || options.find(o => norm(o.value) === target)
    || options.find(o => norm(o.text).includes(target));
  if (!match) return { error: "no-option", options: options.map(o => o.text.trim()).slice(0, 30) };
  if (match.disabled) return { error: "disabled-option", chosen: match.text.trim() };
  this.focus();
  match.selected = true;
  this.dispatchEvent(new Event("input", { bubbles: true }));
  this.dispatchEvent(new Event("change", { bubbles: true }));
  return { chosen: match.text.trim() };
}
"""

QUIET_JS = """
new Promise(resolve => {
  let timer = setTimeout(done, %(quiet)d);
  const cap = setTimeout(done, %(cap)d);
  const observer = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, %(quiet)d); });
  observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  function done() { observer.disconnect(); clearTimeout(timer); clearTimeout(cap); resolve(true); }
})
"""

HUMAN_JS = r"""
(() => {
  const seen = el => el.checkVisibility ? el.checkVisibility() : !!el.offsetParent;
  const captcha = document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], ' +
    'iframe[src*="challenges.cloudflare.com"], .g-recaptcha, .h-captcha, .cf-turnstile');
  if (captcha && seen(captcha)) return { kind: "captcha", reason: "The page shows a CAPTCHA." };
  const empty = selector => [...document.querySelectorAll(selector)].find(el => seen(el) && !el.value);
  if (empty('input[type=password]')) return { kind: "login", reason: "The page asks for a password." };
  if (empty('input[autocomplete^="cc-"]')) return { kind: "payment", reason: "The page asks for card details." };
  if (empty('input[autocomplete="one-time-code"]')) return { kind: "code", reason: "The page asks for a one-time code." };
  return null;
})()
"""

HANDOFF_JS = r"""
((ref, kind, message) => {
  const fox = globalThis.__fox;
  const seen = el => el.checkVisibility ? el.checkVisibility() : !!el.offsetParent;
  const pick = selector => [...document.querySelectorAll(selector)].find(seen) || null;
  const SELECTORS = {
    login: 'input[type=password]',
    payment: 'input[autocomplete^="cc-"]',
    code: 'input[autocomplete="one-time-code"]',
    captcha: 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], ' +
             '.g-recaptcha, .h-captcha, .cf-turnstile',
  };
  const byRef = ref != null && fox && fox.refs.get(ref) ? fox.refs.get(ref).deref() : null;
  const target = byRef && byRef.isConnected ? byRef : (SELECTORS[kind] ? pick(SELECTORS[kind]) : null);
  const state = globalThis.__foxHandoff || (globalThis.__foxHandoff = { answer: null });
  state.target = target;
  document.getElementById("__fox_handoff")?.remove();
  const host = document.createElement("div");
  host.id = "__fox_handoff";
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
  const root = host.attachShadow({ mode: "closed" });
  state.root = root;
  root.innerHTML = `<style>
    .bar { position: fixed; top: 14px; left: 50%; transform: translateX(-50%); pointer-events: auto;
           display: flex; align-items: center; gap: 12px; max-width: min(760px, calc(100vw - 32px));
           padding: 10px 12px 10px 16px; border-radius: 10px; background: #1b1b1b; color: #f2efe8;
           font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
           box-shadow: 0 8px 28px rgba(0,0,0,.35); border: 1px solid #d4a24c; }
    .fox { color: #d4a24c; font-weight: 700; letter-spacing: .02em; }
    .msg { flex: 1; }
    button { font: inherit; border-radius: 7px; padding: 6px 14px; cursor: pointer; border: 1px solid #d4a24c; }
    .done { background: #d4a24c; color: #1b1b1b; font-weight: 600; }
    .skip { background: transparent; color: #f2efe8; border-color: #555; }
    .ring { position: fixed; display: none; border: 3px solid #d4a24c; border-radius: 8px;
            box-shadow: 0 0 0 9999px rgba(0,0,0,.28); pointer-events: none; transition: all .08s; }
  </style>
  <div class="ring"></div>
  <div class="bar"><span class="fox">Fox</span><span class="msg"></span>
    <button class="done">Done</button><button class="skip">Skip</button></div>`;
  root.querySelector(".msg").textContent = message;
  root.querySelector(".done").addEventListener("click", () => { state.answer = "done"; });
  root.querySelector(".skip").addEventListener("click", () => { state.answer = "skip"; });
  document.documentElement.appendChild(host);
  const ring = root.querySelector(".ring");
  if (target) {
    target.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    if (target.matches("input, textarea, select") && !target.disabled) target.focus({ preventScroll: true });
    const place = () => {
      if (!host.isConnected) return;
      const r = target.getBoundingClientRect();
      const shown = target.isConnected && (r.width > 0 || r.height > 0);
      ring.style.display = shown ? "block" : "none";
      if (shown) {
        ring.style.left = `${r.left - 6}px`; ring.style.top = `${r.top - 6}px`;
        ring.style.width = `${r.width + 12}px`; ring.style.height = `${r.height + 12}px`;
      }
      requestAnimationFrame(place);
    };
    place();
  }
  return !!target;
})
"""

HANDOFF_POLL_JS = r"""
(() => {
  const state = globalThis.__foxHandoff;
  const target = state && state.target;
  return {
    answer: state ? state.answer : null,
    present: !!document.getElementById("__fox_handoff"),
    origin: performance.timeOrigin,
    url: location.href,
    files: !!(target && target.isConnected && target.files && target.files.length),
    need: __HUMAN_JS__,
  };
})()
"""

HANDOFF_REMOVE_JS = 'document.getElementById("__fox_handoff")?.remove(); delete globalThis.__foxHandoff;'


class ActionError(Exception):
    pass


class StaleElement(ActionError):
    pass


@dataclass(frozen=True)
class HumanNeeded:
    kind: str
    reason: str


class NeedsHuman(ActionError):
    def __init__(self, kind: str, reason: str, ref: int | None = None):
        super().__init__(reason)
        self.need = HumanNeeded(kind, reason)
        self.ref = ref


@dataclass
class ActionResult:
    text: str
    human: HumanNeeded | None = None


def parse_keys(combo: str) -> tuple[int, tuple[str, str, int, str]]:
    parts = [p.strip() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ActionError("No key given.")
    modifiers = 0
    for part in parts[:-1]:
        bit = MODIFIERS.get(part.lower())
        if bit is None:
            raise ActionError(f"Unknown modifier {part!r}. Use ctrl, shift, alt or meta.")
        modifiers |= bit
    last = parts[-1]
    name = KEY_ALIASES.get(last.lower(), last.lower())
    if name in KEYS:
        key = KEYS[name]
    elif name in MODIFIERS and len(parts) == 1:
        key, code, keycode = MODIFIER_KEYS[MODIFIERS[name]]
        return 0, (key, code, keycode, "")
    elif len(last) == 1:
        char = last.upper() if modifiers & 8 else last
        if last.isalpha():
            code = f"Key{last.upper()}"
        elif last.isdigit():
            code = f"Digit{last}"
        else:
            code = ""
        key = (char, code, ord(last.upper()), char)
    else:
        raise ActionError(f"Unknown key {last!r}.")
    if modifiers & (1 | 2 | 4):
        key = (*key[:3], "")
    return modifiers, key


class Actions:
    def __init__(self, page: Page, snapshotter: Snapshotter | None = None):
        self.page = page
        self.snapshotter = snapshotter or Snapshotter(page)
        self.dialog: dict | None = None
        self.chooser: dict | None = None
        self.popups: list[str] = []
        self.notes: list[str] = []
        self.handoff: dict | None = None
        page.call("Page.setInterceptFileChooserDialog", {"enabled": True})
        page.conn.send("Target.setDiscoverTargets", {"discover": True})
        self._unsubscribe = [
            page.on("Page.javascriptDialogOpening", self._on_dialog),
            page.on("Page.javascriptDialogClosed", self._on_dialog_closed),
            page.on("Page.fileChooserOpened", self._on_chooser),
            page.conn.on("Target.targetCreated", self._on_target),
        ]

    def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []

    def _on_dialog(self, params: dict) -> None:
        if self.handoff is not None:
            return
        kind = params.get("type", "alert")
        if kind in ("alert", "beforeunload"):
            self.notes.append(f"The page showed {'an alert' if kind == 'alert' else 'a leave-page warning'}: "
                              f"{json.dumps(params.get('message', ''))}. Fox accepted it.")
            self.page.post("Page.handleJavaScriptDialog", {"accept": True})
            return
        self.dialog = params

    def _on_dialog_closed(self, params: dict) -> None:
        self.dialog = None

    def _on_chooser(self, params: dict) -> None:
        self.chooser = params

    def _on_target(self, params: dict) -> None:
        if self.handoff is not None:
            return
        info = params.get("targetInfo", {})
        if info.get("type") == "page" and info.get("openerId") == self.page.target_id:
            self.popups.append(info["targetId"])

    def dialog_notice(self) -> str:
        dialog = self.dialog or {}
        kind = dialog.get("type", "dialog")
        extra = ' and text="..." to fill it in' if kind == "prompt" else ""
        return (f"The page is showing a {kind} dialog: {json.dumps(dialog.get('message', ''))}. "
                f"Nothing else on the page works until it is answered: call dialog with accept true or false{extra}.")

    def navigate(self, url: str) -> ActionResult:
        self._check_url(url)
        self._no_dialog()
        with self._settling():
            self.page.navigate(url, timeout=30)
        return self._finish(f"Opened {url}.")

    def back(self) -> ActionResult:
        return self._history(-1, "back")

    def forward(self) -> ActionResult:
        return self._history(1, "forward")

    def click(self, ref: int) -> ActionResult:
        info, element = self._prepare(ref)
        desc = self.snapshotter.describe(ref)
        try:
            if info["disabled"]:
                raise ActionError(f"{desc} is disabled, so clicking it does nothing.")
            if info["covered"]:
                raise ActionError(f"{desc} is covered by {info['covered']}. Close or scroll past that first.")
            with self._settling():
                if info["width"] == 0 or info["height"] == 0:
                    self.page.call_on(element, "function () { this.click(); }", abort_if=self._dialog_open)
                    self.notes.append("It has no size on screen, so Fox clicked it with JavaScript.")
                else:
                    self._mouse_click(info["x"], info["y"])
        finally:
            self.page.release(element)
        return self._finish(f"Clicked {desc}.")

    def type(self, ref: int, text: str, submit: bool = False) -> ActionResult:
        info, element = self._prepare(ref)
        desc = self.snapshotter.describe(ref)
        try:
            if info["password"]:
                raise NeedsHuman("login", f"{desc} is a password field. Fox doesn't type passwords; the human does.",
                                 ref=ref)
            if info["payment"]:
                raise NeedsHuman("payment", f"{desc} asks for card details. Fox doesn't type those; the human does.",
                                 ref=ref)
            if info["code"]:
                raise NeedsHuman("code", f"{desc} asks for a one-time code, which only the human has.", ref=ref)
            if not info["editable"]:
                raise ActionError(f"{desc} isn't a text field. Use click, select or check instead.")
            if info["disabled"]:
                raise ActionError(f"{desc} is disabled.")
            with self._settling():
                self.page.call_on(element, CLEAR_JS)
                if text:
                    self.page.call("Input.insertText", {"text": text}, timeout=INPUT_TIMEOUT)
                else:
                    self._key(0, KEYS["backspace"])
                if submit:
                    self._key(0, KEYS["enter"])
        finally:
            self.page.release(element)
        action = f"Typed {json.dumps(text)} into {desc}" if text else f"Cleared {desc}"
        return self._finish(action + (" and pressed Enter." if submit else "."))

    def select(self, ref: int, option: str) -> ActionResult:
        info, element = self._prepare(ref)
        desc = self.snapshotter.describe(ref)
        try:
            with self._settling():
                outcome = self.page.call_on(element, SELECT_JS, option)
        finally:
            self.page.release(element)
        if outcome.get("error") == "not-select":
            raise ActionError(f"{desc} isn't a dropdown list. If it is a custom menu, click it, then click the option.")
        if outcome.get("error") == "no-option":
            raise ActionError(f"{desc} has no option {json.dumps(option)}. Its options: {json.dumps(outcome['options'])}.")
        if outcome.get("error") == "disabled-option":
            raise ActionError(f"The option {json.dumps(outcome['chosen'])} in {desc} is disabled.")
        return self._finish(f"Chose {json.dumps(outcome['chosen'])} in {desc}.")

    def check(self, ref: int, on: bool = True) -> ActionResult:
        info, element = self._prepare(ref)
        self.page.release(element)
        desc = self.snapshotter.describe(ref)
        if info["checked"] == on:
            return self._finish(f"{desc} was already {'ticked' if on else 'unticked'}.")
        if not on and info["type"] == "radio":
            raise ActionError(f"{desc} is a radio button; untick it by choosing another option.")
        return self.click(ref)

    def hover(self, ref: int) -> ActionResult:
        info, element = self._prepare(ref)
        self.page.release(element)
        desc = self.snapshotter.describe(ref)
        with self._settling():
            self._input("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": info["x"], "y": info["y"]})
        return self._finish(f"Moved the mouse over {desc}.")

    def press(self, combo: str) -> ActionResult:
        self._no_dialog()
        modifiers, key = parse_keys(combo)
        with self._settling():
            self._key(modifiers, key)
        return self._finish(f"Pressed {combo}.")

    def scroll(self, direction: str = "down", ref: int | None = None, amount: float = 0.8) -> ActionResult:
        if ref is not None:
            _, element = self._prepare(ref)
            self.page.release(element)
            return self._finish(f"Scrolled {self.snapshotter.describe(ref)} into view.")
        self._no_dialog()
        direction = direction.lower()
        metrics = self.page.call("Page.getLayoutMetrics")["cssVisualViewport"]
        width, height = metrics["clientWidth"], metrics["clientHeight"]
        with self._settling():
            if direction in ("top", "bottom"):
                self.page.evaluate("window.scrollTo(0, %s)" % ("0" if direction == "top" else
                                                                "document.documentElement.scrollHeight"))
            else:
                deltas = {"down": (0, height * amount), "up": (0, -height * amount),
                          "right": (width * amount, 0), "left": (-width * amount, 0)}
                if direction not in deltas:
                    raise ActionError("Scroll direction must be up, down, left, right, top or bottom.")
                dx, dy = deltas[direction]
                self._input("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": width / 2, "y": height / 2,
                                                         "deltaX": dx, "deltaY": dy})
        return self._finish(f"Scrolled {direction}.")

    def upload(self, ref: int | None, files: str | list[str]) -> ActionResult:
        try:
            chosen = [self._project_file(f) for f in ([files] if isinstance(files, str) else files)]
        except NeedsHuman as e:
            raise NeedsHuman(e.need.kind, str(e), ref=ref) from None
        if not chosen:
            raise ActionError("No file given.")
        target: dict
        multiple = False
        desc = "the file picker"
        element = None
        if ref is not None:
            info, element = self._prepare(ref)
            desc = self.snapshotter.describe(ref)
            if info["tag"] == "INPUT" and info["type"] == "file":
                target = {"objectId": element}
                multiple = info["multiple"]
            elif self.chooser:
                self.page.release(element)
                element = None
                target = {"backendNodeId": self.chooser["backendNodeId"]}
                multiple = self.chooser.get("mode") == "selectMultiple"
            else:
                self.page.release(element)
                raise ActionError(f"{desc} isn't a file input. Click the upload button first; "
                                  "Fox catches the file picker it opens.")
        elif self.chooser:
            target = {"backendNodeId": self.chooser["backendNodeId"]}
            multiple = self.chooser.get("mode") == "selectMultiple"
        else:
            raise ActionError("No file picker is open. Give the number of a file input, or click the upload button first.")
        try:
            if len(chosen) > 1 and not multiple:
                raise ActionError(f"{desc} takes one file, and {len(chosen)} were given.")
            with self._settling():
                self.page.call("DOM.setFileInputFiles", {"files": [str(p) for p in chosen], **target})
        finally:
            if element is not None:
                self.page.release(element)
        self.chooser = None
        names = ", ".join(p.name for p in chosen)
        return self._finish(f"Attached {names} to {desc}.")

    def answer_dialog(self, accept: bool, text: str | None = None) -> ActionResult:
        if not self.dialog:
            raise ActionError("No dialog is open.")
        dialog = self.dialog
        params = {"accept": bool(accept)}
        if text is not None and dialog.get("type") == "prompt":
            params["promptText"] = text
        with self._settling():
            self.page.call("Page.handleJavaScriptDialog", params)
            self.dialog = None
        verb = "Accepted" if accept else "Dismissed"
        filled = f" with {json.dumps(text)}" if "promptText" in params else ""
        return self._finish(f"{verb} the {dialog.get('type')} {json.dumps(dialog.get('message', ''))}{filled}.")

    def hand_to_human(self) -> None:
        self.page.show()

    def begin_handoff(self, kind: str, message: str, ref: int | None = None) -> None:
        self.handoff = {"kind": kind, "message": message, "ref": ref, "origin": None, "url": None}
        self.chooser = None
        self.page.call("Page.setInterceptFileChooserDialog", {"enabled": False})
        self.page.show()
        self._show_bar()
        state = self._poll_state()
        if state is not None:
            self.handoff.update(origin=state["origin"], url=state["url"])

    def poll_handoff(self) -> str | None:
        handoff = self.handoff
        state = self._poll_state()
        if state is None:
            return None
        if state["answer"] in ("done", "skip"):
            return state["answer"]
        if not state["present"]:
            self._show_bar()
        kind = handoff["kind"]
        moved = state["origin"] != handoff["origin"] or state["url"] != handoff["url"]
        need = (state["need"] or {}).get("kind")
        if kind == "captcha" and need != "captcha":
            return "finished"
        if kind in ("login", "payment", "code") and moved and need != kind:
            return "finished"
        if kind == "file" and state["files"]:
            return "finished"
        return None

    def end_handoff(self, lead: str) -> ActionResult:
        try:
            self.page.evaluate(HANDOFF_REMOVE_JS, context_id=self.snapshotter.world, timeout=3)
        except CDPError:
            pass
        self.handoff = None
        self.popups = []
        self.chooser = None
        self.dialog = None
        try:
            self.page.call("Page.setInterceptFileChooserDialog", {"enabled": True})
        except CDPError:
            pass
        self.page.hide()
        self.snapshotter.forget()
        return self._finish(lead, full=True)

    def _show_bar(self) -> None:
        handoff = self.handoff
        call = "%s(%s, %s, %s)" % (HANDOFF_JS.strip(), json.dumps(handoff["ref"]), json.dumps(handoff["kind"]),
                                   json.dumps(handoff["message"]))
        for attempt in range(2):
            try:
                if self.snapshotter.world is None:
                    self.snapshotter.take()
                self.page.evaluate(call, context_id=self.snapshotter.world, timeout=5)
                handoff["ref"] = handoff["ref"] if attempt == 0 else None
                return
            except CDPTimeout:
                return
            except CDPError as e:
                if attempt or "context" not in e.message.lower():
                    raise
                handoff["ref"] = None
                self.snapshotter.take()

    def _poll_state(self) -> dict | None:
        script = HANDOFF_POLL_JS.replace("__HUMAN_JS__", HUMAN_JS.strip())
        try:
            return self.page.evaluate(script, context_id=self.snapshotter.world, timeout=3)
        except CDPTimeout:
            return None
        except CDPError as e:
            if "context" not in e.message.lower():
                raise
            try:
                self.snapshotter.take()
            except CDPError:
                return None
            self.handoff["ref"] = None
            self._show_bar()
            return None

    def snapshot(self, full: bool = False) -> ActionResult:
        if self.dialog:
            return ActionResult(self.dialog_notice())
        return self._finish(None, full=full)

    def _dialog_open(self) -> bool:
        return self.dialog is not None

    def _no_dialog(self) -> None:
        if self.dialog:
            raise ActionError(self.dialog_notice())

    def _check_url(self, url: str) -> None:
        scheme = urlparse(url).scheme.lower()
        if scheme == "file":
            path = Path(urlparse(url).path.lstrip("/"))
            try:
                self._project_file(str(path))
            except NeedsHuman as e:
                raise ActionError(f"Fox only opens files inside the project; {path} is outside it.") from e
            return
        if scheme not in ALLOWED_SCHEMES:
            raise ActionError(f"Fox only opens http, https and project files, not {scheme or 'that'}: URLs.")

    def _project_file(self, name: str) -> Path:
        workspace = paths.workspace().resolve()
        path = Path(name)
        resolved = (path if path.is_absolute() else workspace / path).resolve()
        if not resolved.is_relative_to(workspace):
            raise NeedsHuman("file", f"{name} is outside the project folder. Fox only hands project files "
                                     "to websites; the human can pick this one.")
        if not resolved.is_file():
            raise ActionError(f"{name} doesn't exist in the project.")
        return resolved

    def _history(self, step: int, word: str) -> ActionResult:
        self._no_dialog()
        history = self.page.call("Page.getNavigationHistory")
        index = history["currentIndex"] + step
        if not 0 <= index < len(history["entries"]):
            raise ActionError(f"There is no page to go {word} to.")
        entry = history["entries"][index]
        with self._settling(expect_load=True):
            self.page.call("Page.navigateToHistoryEntry", {"entryId": entry["id"]})
        return self._finish(f"Went {word} to {entry['url']}.")

    def _prepare(self, ref: int) -> tuple[dict, str]:
        self._no_dialog()
        world = self.snapshotter.world
        stale = f"Element [{ref}] is from an older page. Take a new snapshot and use its numbers."
        if world is None:
            raise StaleElement("There are no element numbers yet. Take a snapshot first.")
        try:
            element = self.page.evaluate_handle(
                f"(globalThis.__fox && globalThis.__fox.refs.get({int(ref)})?.deref()) || null", context_id=world
            )
        except CDPError as e:
            if "context" in e.message.lower():
                raise StaleElement(stale) from None
            raise
        if element is None:
            raise StaleElement(f"There is no element [{ref}] on the page now. Take a new snapshot.")
        info = self.page.call_on(element, PREPARE_JS)
        if info.get("gone"):
            self.page.release(element)
            raise StaleElement(f"Element [{ref}] has left the page. Take a new snapshot.")
        return info, element

    def _input(self, method: str, params: dict) -> None:
        try:
            self.page.call(method, params, timeout=INPUT_TIMEOUT, abort_if=self._dialog_open)
        except CDPAborted:
            pass

    def _mouse_click(self, x: float, y: float) -> None:
        self._input("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            if self.dialog:
                return
            self._input("Input.dispatchMouseEvent",
                        {"type": kind, "x": x, "y": y, "button": "left", "buttons": buttons, "clickCount": 1})

    def _key(self, modifiers: int, key: tuple[str, str, int, str]) -> None:
        held = 0
        for bit, (name, code, keycode) in MODIFIER_KEYS.items():
            if modifiers & bit:
                held |= bit
                self._input("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": name, "code": code,
                                                       "windowsVirtualKeyCode": keycode, "modifiers": held})
        name, code, keycode, text = key
        down = {"type": "keyDown" if text else "rawKeyDown", "key": name, "code": code,
                "windowsVirtualKeyCode": keycode, "modifiers": modifiers}
        if text:
            down.update(text=text, unmodifiedText=text)
        self._input("Input.dispatchKeyEvent", down)
        if self.dialog:
            return
        self._input("Input.dispatchKeyEvent", {"type": "keyUp", "key": name, "code": code,
                                               "windowsVirtualKeyCode": keycode, "modifiers": modifiers})
        for bit, (name, code, keycode) in reversed(MODIFIER_KEYS.items()):
            if modifiers & bit:
                held &= ~bit
                self._input("Input.dispatchKeyEvent", {"type": "keyUp", "key": name, "code": code,
                                                       "windowsVirtualKeyCode": keycode, "modifiers": held})

    @contextmanager
    def _settling(self, expect_load: bool = False):
        main = self.page.target_id
        with self.page.expect("Page.frameStartedLoading") as started, \
                self.page.expect("Page.loadEventFired") as loaded:
            yield
            if self.dialog:
                return
            navigating = expect_load
            if not navigating:
                try:
                    started.wait(NAVIGATION_GRACE, where=lambda e: e.get("frameId") == main)
                    navigating = True
                except CDPTimeout:
                    pass
            if navigating:
                try:
                    loaded.wait(SETTLE_TIMEOUT)
                except CDPTimeout:
                    self.notes.append(f"The page was still loading after {SETTLE_TIMEOUT:g}s.")
        if self.dialog:
            return
        try:
            self.page.evaluate(QUIET_JS % {"quiet": QUIET_MS, "cap": int(SETTLE_TIMEOUT * 1000)},
                               timeout=SETTLE_TIMEOUT + 2)
        except CDPError:
            pass
        self._adopt_popups()

    def _adopt_popups(self) -> None:
        while self.popups:
            target = self.popups.pop(0)
            url = ""
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                infos = self.page.conn.send("Target.getTargets")["targetInfos"]
                url = next((t["url"] for t in infos if t["targetId"] == target), "")
                if url and url != "about:blank":
                    break
                time.sleep(0.1)
            try:
                self.page.conn.send("Target.closeTarget", {"targetId": target})
            except CDPError:
                pass
            if url and url != "about:blank":
                self.notes.append(f"It tried to open {url} in a new tab; Fox opened it in this tab instead.")
                try:
                    self.page.navigate(url, timeout=30)
                except CDPError as e:
                    self.notes.append(f"Opening it failed: {e}")
            else:
                self.notes.append("It opened an empty new tab, which Fox closed.")

    def _human_check(self) -> HumanNeeded | None:
        world = self.snapshotter.world
        try:
            found = self.page.evaluate(HUMAN_JS, context_id=world)
        except CDPError:
            return None
        return HumanNeeded(found["kind"], found["reason"]) if found else None

    def _finish(self, message: str | None, full: bool = False) -> ActionResult:
        parts = [message] if message else []
        parts += self.notes
        self.notes = []
        if self.dialog:
            parts.append(self.dialog_notice())
            return ActionResult("\n".join(parts))
        if self.chooser:
            parts.append("A file picker opened. Call upload with a file from the project to fill it.")
        view = self.snapshotter.full() if full else self.snapshotter.update()
        human = self._human_check()
        if human:
            parts.append(f"{human.reason} Fox doesn't fill in passwords, card details, codes or CAPTCHAs. "
                         "If this step needs one, hand the page to the human.")
        if parts:
            parts.append("")
        parts.append(view)
        return ActionResult("\n".join(parts), human)
