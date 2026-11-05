import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from browser.cdp import CDPError, JavaScriptError, Page

MAX_NODES = 3000
DEFAULT_BUDGET = 6000
MAX_INDENT = 8
DIFF_FALLBACK_RATIO = 0.6
MAX_URL = 200
GAP = "…"
GAP_COST = len(GAP) + 1

WALKER_JS = r"""
(() => {
  const MAX_NODES = __MAX_NODES__;
  const fox = globalThis.__fox || (globalThis.__fox = {
    ids: new WeakMap(), refs: new Map(), next: 1, doc: Math.random().toString(36).slice(2),
  });
  fox.refs = new Map();
  const vw = innerWidth, vh = innerHeight;
  const active = document.activeElement;
  const nodes = [];
  let pending = [], pendingDepth = 0, pendingInView = false, truncated = false, suppress = 0;

  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK", "TITLE"]);
  const BUTTON_TYPES = new Set(["button", "submit", "reset", "image"]);
  const INTERACTIVE = new Set(["button", "link", "checkbox", "radio", "switch", "tab", "menuitem",
    "menuitemcheckbox", "menuitemradio", "option", "textbox", "searchbox", "combobox", "slider",
    "spinbutton", "listbox", "treeitem", "file", "clickable"]);
  const LEAVES = new Set(["img", "iframe", "progressbar", "meter"]);
  const CONTAINERS = new Set(["navigation", "main", "banner", "contentinfo", "complementary", "form",
    "dialog", "alertdialog", "list", "listitem", "table", "row", "region", "menu", "menubar", "tablist",
    "tree", "grid", "group", "radiogroup", "alert", "toolbar", "heading"]);
  const LANDMARKS = { NAV: "navigation", MAIN: "main", HEADER: "banner", FOOTER: "contentinfo",
    ASIDE: "complementary", FORM: "form", DIALOG: "dialog" };
  const HAS_INTERACTIVE = "a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[tabindex]";

  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;
  const inViewport = r => (r.width > 0 || r.height > 0) && r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
  const textOf = el => clean(el.innerText ?? el.textContent);

  function push(node) {
    if (nodes.length >= MAX_NODES) { truncated = true; return false; }
    nodes.push(node);
    return true;
  }

  function flush() {
    if (!pending.length) return;
    const text = clip(clean(pending.join("")), 300);
    pending = [];
    if (/[\p{L}\p{N}]/u.test(text)) push({ role: "text", name: text, depth: pendingDepth, inView: pendingInView });
  }

  function roleOf(el, tag) {
    const explicit = clean(el.getAttribute("role")).split(" ")[0];
    if (explicit && !["presentation", "none", "generic"].includes(explicit)) return explicit;
    switch (tag) {
      case "A": case "AREA": return el.hasAttribute("href") ? "link" : null;
      case "BUTTON": case "SUMMARY": return "button";
      case "SELECT": return el.multiple || el.size > 1 ? "listbox" : "combobox";
      case "TEXTAREA": return "textbox";
      case "INPUT": {
        const t = (el.getAttribute("type") || "").toLowerCase();
        if (t === "hidden") return null;
        if (BUTTON_TYPES.has(t) || t === "color") return "button";
        if (t === "checkbox" || t === "radio") return t;
        if (t === "range") return "slider";
        if (t === "file") return "file";
        if (t === "number") return "spinbutton";
        if (t === "search") return "searchbox";
        return "textbox";
      }
      case "H1": case "H2": case "H3": case "H4": case "H5": case "H6": return "heading";
      case "IMG": return clean(el.getAttribute("alt")) ? "img" : null;
      case "UL": case "OL": case "MENU": return "list";
      case "LI": return "listitem";
      case "TABLE": return "table";
      case "TR": return "row";
      case "IFRAME": return "iframe";
      case "PROGRESS": return "progressbar";
      case "METER": return "meter";
    }
    if (LANDMARKS[tag]) return LANDMARKS[tag];
    if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) return "textbox";
    return null;
  }

  function looksClickable(el, style) {
    if (el.hasAttribute("onclick")) return true;
    const tab = el.getAttribute("tabindex");
    if (tab !== null && +tab >= 0) return true;
    if (style.cursor !== "pointer") return false;
    const parent = el.parentElement;
    return !parent || getComputedStyle(parent).cursor !== "pointer";
  }

  function byIds(el, attr) {
    const ids = clean(el.getAttribute(attr));
    if (!ids) return "";
    return clean(ids.split(" ").map(id => {
      const target = el.ownerDocument.getElementById(id);
      return target ? textOf(target) : "";
    }).join(" "));
  }

  function nameOf(el, tag, role) {
    const aria = byIds(el, "aria-labelledby") || clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      const label = el.labels && el.labels.length ? textOf(el.labels[0]) : "";
      if (label) return label;
      if (tag === "INPUT" && BUTTON_TYPES.has(el.type)) {
        return clean(el.value || el.alt || (el.type === "submit" ? "Submit" : el.type === "reset" ? "Reset" : ""));
      }
      return clean(el.getAttribute("title"));
    }
    if (tag === "IMG") return clean(el.getAttribute("alt"));
    if (tag === "IFRAME") return clean(el.getAttribute("title") || el.getAttribute("name"));
    if (role === "textbox") return clean(el.getAttribute("title"));
    const text = textOf(el);
    if (text) return text;
    const img = el.querySelector("img[alt]");
    if (img && clean(img.alt)) return clean(img.alt);
    const svg = el.querySelector("svg[aria-label], svg title");
    if (svg) return clean(svg.getAttribute("aria-label") || svg.textContent);
    const titled = el.hasAttribute("title") ? el : el.querySelector("[title]");
    return titled ? clean(titled.getAttribute("title")) : "";
  }

  function hrefOf(el) {
    const href = el.href;
    if (typeof href !== "string" || !href || href.startsWith("javascript:")) return null;
    try {
      const url = new URL(href);
      return url.origin === location.origin ? url.pathname + url.search + url.hash : clip(href, 100);
    } catch (e) {
      return null;
    }
  }

  function propsOf(el, tag, role) {
    const p = {};
    if (role === "heading") p.level = +tag[1] || +el.getAttribute("aria-level") || 2;
    if (role === "link") { const href = hrefOf(el); if (href) p.href = href; }
    if (tag === "INPUT" || tag === "TEXTAREA") {
      if (role === "checkbox" || role === "radio") {
        p.checked = el.indeterminate ? "mixed" : el.checked;
      } else if (role === "file") {
        p.files = el.files ? el.files.length : 0;
      } else if (role !== "button") {
        const auto = (el.getAttribute("autocomplete") || "").toLowerCase();
        const hint = [el.name, el.id, el.getAttribute("aria-label"), el.placeholder].join(" ").toLowerCase();
        const secret = el.type === "password" || auto.startsWith("cc-") || auto === "one-time-code"
          || /card.?(number|no)|cvc|cvv|security.?code/.test(hint);
        p.value = secret ? "*".repeat(Math.min(el.value.length, 8)) : clip(el.value, 200);
        if (!el.value && el.placeholder) p.placeholder = clip(clean(el.placeholder), 80);
      }
    } else if (tag === "SELECT") {
      p.value = clip([...el.selectedOptions].map(o => clean(o.text)).join(", "), 200);
      p.options = el.options.length;
    } else if (role === "textbox") {
      p.value = clip(textOf(el), 200);
    }
    for (const [attr, key] of [["aria-checked", "checked"], ["aria-pressed", "pressed"],
                               ["aria-expanded", "expanded"], ["aria-selected", "selected"]]) {
      const v = el.getAttribute(attr);
      if (v === "true" || v === "false") p[key] = v === "true";
      else if (v === "mixed") p[key] = "mixed";
    }
    if (el.disabled || el.getAttribute("aria-disabled") === "true") p.disabled = true;
    if (el.required || el.getAttribute("aria-required") === "true") p.required = true;
    try { if (el.matches(":user-invalid")) p.invalid = true; } catch (e) {}
    if (el === active && el !== document.body) p.focused = true;
    return p;
  }

  function refFor(el) {
    let id = fox.ids.get(el);
    if (!id) { id = fox.next++; fox.ids.set(el, id); }
    fox.refs.set(id, new WeakRef(el));
    return id;
  }

  function walkList(list, depth, visible) {
    for (const child of list) { if (truncated) return; walk(child, depth, visible); }
  }

  function walk(node, depth, visible) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (suppress || !visible) return;
      const text = node.data;
      if (!text.trim()) { if (pending.length) pending.push(" "); return; }
      if (!pending.length) {
        pendingDepth = depth;
        const parent = node.parentElement;
        pendingInView = parent ? inViewport(parent.getBoundingClientRect()) : false;
      }
      pending.push(text);
      return;
    }
    if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) { walkList(node.childNodes, depth, visible); return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node, tag = el.tagName;
    if (SKIP.has(tag) || el instanceof SVGElement) return;
    const style = getComputedStyle(el);
    if (style.display === "none") return;
    if (tag === "SLOT") {
      const assigned = el.assignedNodes({ flatten: true });
      walkList(assigned.length ? assigned : el.childNodes, depth, visible);
      return;
    }
    const block = !style.display.startsWith("inline") && style.display !== "contents";
    if (block) flush();
    const shown = style.visibility === "visible";
    let role = shown ? roleOf(el, tag) : null;
    if (!role && shown && tag !== "LABEL" && looksClickable(el, style) && !el.querySelector(HAS_INTERACTIVE)) {
      role = "clickable";
    }
    if (role === "heading" && !el.querySelector(HAS_INTERACTIVE)) role = "heading-leaf";

    if (role && (INTERACTIVE.has(role) || LEAVES.has(role) || role === "heading-leaf")) {
      flush();
      const realRole = role === "heading-leaf" ? "heading" : role;
      const entry = { role: realRole, name: clip(nameOf(el, tag, realRole), 150), depth,
        inView: inViewport(el.getBoundingClientRect()), props: propsOf(el, tag, realRole) };
      if (INTERACTIVE.has(realRole)) entry.ref = refFor(el);
      push(entry);
      return;
    }

    const children = el.shadowRoot ? el.shadowRoot.childNodes : el.childNodes;
    if (role && CONTAINERS.has(role)) {
      flush();
      const index = nodes.length;
      const entry = { role, name: clip(clean(el.getAttribute("aria-label")), 150), depth,
        inView: inViewport(el.getBoundingClientRect()), props: propsOf(el, tag, role) };
      if (!push(entry)) return;
      walkList(children, depth + 1, shown);
      flush();
      const kids = nodes.length - index - 1;
      if (kids === 0 && !entry.name) nodes.splice(index, 1);
      else if (kids === 1 && !entry.name && nodes[index + 1].role === "text") {
        entry.name = nodes[index + 1].name;
        nodes.splice(index + 1, 1);
      } else if (kids === 1 && !entry.name && role === "listitem") {
        nodes[index + 1].depth = depth;
        nodes.splice(index, 1);
      }
      return;
    }

    const labelling = tag === "LABEL" && el.control;
    if (labelling) suppress++;
    walkList(children, depth, shown);
    if (labelling) suppress--;
    if (block) flush();
  }

  walk(document.body || document.documentElement, 0, true);
  flush();
  return {
    url: location.href,
    title: document.title,
    doc: fox.doc,
    viewport: [vw, vh],
    scroll: [Math.round(scrollY), Math.round(document.documentElement.scrollHeight)],
    nodes,
    truncated,
  };
})()
"""


@dataclass(frozen=True)
class Node:
    role: str
    name: str
    depth: int
    in_view: bool
    ref: int | None = None
    props: tuple = ()

    @classmethod
    def from_dict(cls, raw: dict) -> "Node":
        return cls(
            role=raw["role"],
            name=raw.get("name", ""),
            depth=raw.get("depth", 0),
            in_view=bool(raw.get("inView")),
            ref=raw.get("ref"),
            props=tuple(raw.get("props", {}).items()),
        )

    def line(self) -> str:
        indent = "  " * min(self.depth, MAX_INDENT)
        if self.role == "text":
            return indent + self.name
        parts = [f"[{self.ref}]"] if self.ref is not None else []
        parts.append(self.role)
        if self.name:
            parts.append(json.dumps(self.name, ensure_ascii=False))
        parts.extend(_prop(key, value) for key, value in self.props)
        return indent + " ".join(p for p in parts if p)


def _prop(key: str, value) -> str:
    if value is True:
        return key
    if value is False:
        return "unchecked" if key == "checked" else f"not {key}"
    if isinstance(value, str):
        return f"{key}={json.dumps(value, ensure_ascii=False)}"
    return f"{key}={value}"


@dataclass
class Snapshot:
    url: str
    title: str
    doc: str
    viewport: tuple[int, int]
    scroll: tuple[int, int]
    nodes: list[Node]
    truncated: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "Snapshot":
        return cls(
            url=raw["url"],
            title=raw.get("title", ""),
            doc=raw["doc"],
            viewport=tuple(raw["viewport"]),
            scroll=tuple(raw["scroll"]),
            nodes=[Node.from_dict(n) for n in raw["nodes"]],
            truncated=bool(raw.get("truncated")),
        )

    def header(self) -> str:
        width, height = self.viewport
        top, total = self.scroll
        return (f"Page: {self.title or '(no title)'}\nURL: {_clip(self.url, MAX_URL)}\n"
                f"Viewport {width}x{height}, scrolled to {top}px of {total}px")


def _priority(node: Node) -> int:
    if node.in_view:
        return 0
    if node.ref is not None or node.role == "heading":
        return 1
    return 2


def render(snapshot: Snapshot, budget: int = DEFAULT_BUDGET) -> tuple[str, set[int]]:
    lines = [node.line() for node in snapshot.nodes]
    count = len(lines)
    order = sorted(range(count), key=lambda i: (_priority(snapshot.nodes[i]), i))
    keep = [False] * count
    used = GAP_COST if count else 0
    for i in order:
        left = i == 0 or keep[i - 1]
        right = i == count - 1 or keep[i + 1]
        gaps = -1 if left and right else (1 if not left and not right else 0)
        cost = len(lines[i]) + 1 + gaps * GAP_COST
        if used + cost > budget:
            continue
        keep[i] = True
        used += cost
    kept = {i for i in range(count) if keep[i]}
    body = []
    for i, line in enumerate(lines):
        if keep[i]:
            body.append(line)
        elif i == 0 or keep[i - 1]:
            body.append(GAP)
    parts = [snapshot.header(), "", *body]
    if not lines:
        parts.append("(the page has no visible content)")
    if len(kept) < len(lines):
        refs_total = sum(1 for n in snapshot.nodes if n.ref is not None)
        refs_shown = sum(1 for i in kept if snapshot.nodes[i].ref is not None)
        parts += ["", f"Showing {len(kept)} of {len(lines)} lines ({refs_shown} of {refs_total} numbered "
                      "elements). Scroll, or ask for a bigger budget, to see the rest."]
    if snapshot.truncated:
        parts.append(f"The page has more than {MAX_NODES} elements; the rest were not read.")
    return "\n".join(parts), kept


def _clip(text: str, limit: int = 80) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def diff(old: Snapshot, new: Snapshot) -> list[str] | None:
    if old.doc != new.doc:
        return None
    out = []
    if old.url != new.url:
        out.append(f"URL: {_clip(new.url, MAX_URL)}")
    if old.title != new.title:
        out.append(f"Title: {new.title}")
    if old.scroll != new.scroll:
        out.append(f"Scrolled to {new.scroll[0]}px of {new.scroll[1]}px")
    a = [n.line() for n in old.nodes]
    b = [n.line() for n in new.nodes]
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        out.append(f"after {_clip(b[j1 - 1])}:" if j1 > 0 else "at the top:")
        old_refs = {old.nodes[i].ref for i in range(i1, i2) if old.nodes[i].ref is not None}
        new_refs = {new.nodes[j].ref for j in range(j1, j2) if new.nodes[j].ref is not None}
        for i in range(i1, i2):
            if old.nodes[i].ref is None or old.nodes[i].ref not in new_refs:
                out.append("- " + a[i].strip())
        for j in range(j1, j2):
            mark = "~ " if new.nodes[j].ref is not None and new.nodes[j].ref in old_refs else "+ "
            out.append(mark + b[j].strip())
    return out


@dataclass
class Snapshotter:
    page: Page
    budget: int = DEFAULT_BUDGET
    last: Snapshot | None = None
    seen: set[str] = field(default_factory=set)
    _world: int | None = None

    @property
    def world(self) -> int | None:
        return self._world

    def describe(self, ref: int) -> str:
        if self.last is not None:
            for node in self.last.nodes:
                if node.ref == ref:
                    return node.line().strip()
        return f"[{ref}]"

    def take(self) -> Snapshot:
        expression = WALKER_JS.replace("__MAX_NODES__", str(MAX_NODES))
        for attempt in range(2):
            if self._world is None:
                self._world = self.page.create_world()
            try:
                return Snapshot.from_dict(self.page.evaluate(expression, context_id=self._world))
            except JavaScriptError:
                raise
            except CDPError as e:
                if attempt or "context" not in e.message.lower():
                    raise
                self._world = None
        raise AssertionError("unreachable")

    def full(self, budget: int | None = None) -> str:
        return self._show_full(self.take(), budget or self.budget)

    def _show_full(self, snapshot: Snapshot, budget: int) -> str:
        text, kept = render(snapshot, budget)
        self.last = snapshot
        self.seen = {snapshot.nodes[i].line() for i in kept}
        return text

    def update(self, budget: int | None = None) -> str:
        budget = budget or self.budget
        if self.last is None:
            return self.full(budget)
        snapshot = self.take()
        changes = diff(self.last, snapshot)
        if changes is None:
            return self._show_full(snapshot, budget)
        printed = {line[2:] for line in changes if line[:2] in ("+ ", "~ ")}
        unseen = [n.line() for n in snapshot.nodes
                  if n.in_view and n.line() not in self.seen and n.line().strip() not in printed]
        body = list(changes)
        if unseen:
            body.append("now in view:")
            body.extend("  " + line.strip() for line in unseen)
        if not body:
            text = "No changes since the last snapshot."
        else:
            text = "Changes since the last snapshot:\n" + "\n".join(body)
        full_text, kept = render(snapshot, budget)
        if len(text) > DIFF_FALLBACK_RATIO * len(full_text):
            self.last = snapshot
            self.seen = {snapshot.nodes[i].line() for i in kept}
            return full_text
        self.last = snapshot
        self.seen |= {n.line() for n in snapshot.nodes if n.line().strip() in printed}
        self.seen |= set(unseen)
        return text

    def forget(self) -> None:
        self.last = None
        self.seen = set()
