import os

from browser.cdp import CDPError, Page
from browser.snapshot import Snapshotter
from calls import timed_generate
from providers import Image, Turn, get_provider, has_vision
from providers.config import MODEL_REGISTRY

JPEG_QUALITY = 70
MAX_PER_KIND = 6
HELPER_ENV = "FOX_VISION_HELPER"

BOXES_JS = r"""
(() => {
  const fox = globalThis.__fox;
  document.getElementById("__fox_boxes")?.remove();
  if (!fox) return 0;
  const layer = document.createElement("div");
  layer.id = "__fox_boxes";
  layer.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
  const colors = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"];
  let drawn = 0;
  for (const [id, ref] of fox.refs) {
    const el = ref.deref();
    if (!el || !el.isConnected) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
    const color = colors[id % colors.length];
    const box = document.createElement("div");
    box.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;` +
      `outline:2px solid ${color};box-sizing:border-box;`;
    const tag = document.createElement("div");
    tag.textContent = id;
    tag.style.cssText = `position:absolute;left:-2px;top:${r.top > 14 ? -16 : 0}px;background:${color};color:#fff;` +
      "font:bold 11px/14px monospace;padding:0 3px;border-radius:2px;";
    box.appendChild(tag);
    layer.appendChild(box);
    drawn++;
  }
  document.documentElement.appendChild(layer);
  return drawn;
})()
"""

REMOVE_BOXES_JS = 'document.getElementById("__fox_boxes")?.remove()'

LAYOUT_JS = r"""
(() => {
  const MAX = __MAX_PER_KIND__;
  const vw = document.documentElement.clientWidth, vh = innerHeight;
  const doc = document.documentElement;
  const fox = globalThis.__fox;
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const refOf = el => {
    if (!fox) return null;
    const id = fox.ids.get(el);
    return id && fox.refs.has(id) ? id : null;
  };
  const describe = el => {
    const ref = refOf(el);
    const label = clean(el.getAttribute("aria-label") || el.innerText || el.getAttribute("alt")
      || el.getAttribute("placeholder") || el.value || "").slice(0, 50);
    let name = el.tagName.toLowerCase();
    if (el.id) name += "#" + el.id;
    else if (typeof el.className === "string" && el.className.trim()) name += "." + el.className.trim().split(/\s+/).slice(0, 2).join(".");
    return (ref ? `[${ref}] ` : "") + name + (label ? ` "${label}"` : "");
  };
  const found = {};
  const counts = {};
  const add = (kind, el, detail) => {
    counts[kind] = (counts[kind] || 0) + 1;
    if ((found[kind] = found[kind] || []).length < MAX) found[kind].push(`${describe(el)} ${detail}`);
  };
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "BR", "HEAD", "META", "LINK", "OPTION"]);
  const INTERACTIVE = "a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[tabindex]";
  const styles = new Map();
  const style = el => { let s = styles.get(el); if (!s) { s = getComputedStyle(el); styles.set(el, s); } return s; };
  const shown = el => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const s = style(node);
      if (s.display === "none" || +s.opacity < 0.05) return false;
    }
    return style(el).visibility === "visible";
  };
  const ownText = el => clean([...el.childNodes].filter(n => n.nodeType === 3).map(n => n.data).join(" "));
  const elements = [];
  for (const el of document.body ? document.body.querySelectorAll("*") : []) {
    if (SKIP.has(el.tagName) || el.closest("svg") && el.tagName.toLowerCase() !== "svg" || el.id === "__fox_boxes") continue;
    elements.push(el);
    if (elements.length >= 5000) break;
  }
  const visible = elements.filter(shown);
  const rects = new Map(visible.map(el => [el, el.getBoundingClientRect()]));
  const scrolls = s => /(auto|scroll|hidden|clip)/.test(s.overflowX + s.overflowY);
  const insideScroller = el => {
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      if (scrolls(style(node))) return true;
    }
    return false;
  };

  const pageWidth = doc.scrollWidth;
  if (pageWidth > vw + 1) {
    for (const el of visible) {
      const r = rects.get(el);
      if (r.width === 0 || r.right <= vw + 1 || insideScroller(el)) continue;
      const parent = el.parentElement;
      const pr = parent && rects.get(parent);
      if (pr && pr.right > vw + 1) continue;
      add("sideways scroll", el, `sticks out ${Math.round(r.right - vw)}px past the right edge, so the page scrolls sideways (${pageWidth}px wide in a ${vw}px window)`);
    }
  }

  for (const el of visible) {
    const r = rects.get(el);
    if (r.width === 0 && r.height === 0) continue;
    if (r.right + scrollX < 0 || r.bottom + scrollY < 0) {
      if (r.width > 2 && r.height > 2 && (ownText(el) || el.matches(INTERACTIVE))) {
        add("off the page", el, `is at x=${Math.round(r.left + scrollX)}, y=${Math.round(r.top + scrollY)}, where nobody can scroll to it`);
      }
    }
  }

  for (const el of visible) {
    const s = style(el);
    const r = rects.get(el);
    if (r.width < 4 || r.height < 4 || !clean(el.innerText)) continue;
    const overX = el.scrollWidth - el.clientWidth, overY = el.scrollHeight - el.clientHeight;
    const hides = v => v === "hidden" || v === "clip";
    if ((hides(s.overflowX) && overX > 2) || (hides(s.overflowY) && overY > 2)) {
      if (s.textOverflow === "ellipsis" || +s.webkitLineClamp > 0) continue;
      const cut = overX > 2 && hides(s.overflowX) ? `${el.clientWidth}px of ${el.scrollWidth}px wide` : `${el.clientHeight}px of ${el.scrollHeight}px tall`;
      add("cut off", el, `hides part of its content (shows ${cut})`);
    } else if (s.overflowX === "visible" && s.overflowY === "visible" && ownText(el) && el.clientWidth > 0) {
      if (overX > 4 && s.whiteSpace !== "nowrap" || overY > 4 && s.height !== "auto" && el.clientHeight > 0) {
        const by = overX > 4 ? `${overX}px to the side` : `${overY}px below`;
        add("text spills out", el, `has text that spills ${by} of its box`);
      }
    }
  }

  const candidates = visible.filter(el => {
    const r = rects.get(el);
    if (r.width < 2 || r.height < 2) return false;
    if (el.matches(INTERACTIVE) || el.tagName === "IMG") return true;
    return ownText(el).length > 0;
  }).slice(0, 500);
  const reported = new Set();
  for (let i = 0; i < candidates.length; i++) {
    const a = candidates[i], ra = rects.get(a);
    for (let j = i + 1; j < candidates.length; j++) {
      const b = candidates[j], rb = rects.get(b);
      if (a.contains(b) || b.contains(a)) continue;
      const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (w <= 2 || h <= 2) continue;
      const smaller = Math.min(ra.width * ra.height, rb.width * rb.height);
      if (w * h < 50 || w * h < 0.25 * smaller) continue;
      if (reported.has(a) || reported.has(b)) continue;
      reported.add(a); reported.add(b);
      add("overlap", a, `overlaps ${describe(b)} by ${Math.round(w)}x${Math.round(h)}px`);
    }
  }

  for (const el of visible) {
    if (!el.matches(INTERACTIVE)) continue;
    const r = rects.get(el);
    const label = clean(el.innerText || el.getAttribute("aria-label") || el.value || "");
    if ((r.width === 0 || r.height === 0) && label && style(el).display !== "contents") {
      add("no size", el, `has ${r.width === 0 ? "zero width" : "zero height"}, so it can't be seen or clicked`);
      continue;
    }
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (r.width === 0 || x < 0 || y < 0 || x >= vw || y >= vh) continue;
    const hit = document.elementFromPoint(x, y);
    if (!hit || hit === el || el.contains(hit) || hit.contains(el)) continue;
    const labelled = hit.closest && hit.closest("label");
    if (labelled && labelled.control === el) continue;
    add("covered", el, `is covered by ${describe(hit)}, so clicks hit that instead`);
  }

  const parse = c => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  };
  const lum = ([r, g, b]) => {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const backdrop = el => {
    let color = [255, 255, 255, 1];
    const layers = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const s = style(node);
      if (s.backgroundImage && s.backgroundImage !== "none") return null;
      const c = parse(s.backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    for (const c of layers.reverse()) color = [0, 1, 2].map(i => c[i] * c[3] + color[i] * (1 - c[3])).concat(1);
    return color;
  };
  for (const el of visible) {
    if (!ownText(el) || el.disabled) continue;
    const s = style(el);
    const fg = parse(s.color), bg = backdrop(el);
    if (!fg || !bg) continue;
    const mixed = [0, 1, 2].map(i => fg[i] * fg[3] + bg[i] * (1 - fg[3]));
    const [l1, l2] = [lum(mixed), lum(bg)].sort((a, b) => b - a);
    const ratio = (l1 + 0.05) / (l2 + 0.05);
    const size = parseFloat(s.fontSize), bold = +s.fontWeight >= 700;
    const need = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (ratio < need) {
      const hex = c => "#" + c.slice(0, 3).map(v => Math.round(v).toString(16).padStart(2, "0")).join("");
      add("low contrast", el, `is ${hex(mixed)} text on ${hex(bg)}: contrast ${ratio.toFixed(1)}:1, needs ${need}:1`);
    }
  }

  for (const img of document.images) {
    if (shown(img) && img.complete && img.naturalWidth === 0 && img.getAttribute("src")) {
      add("broken image", img, `failed to load (${clean(img.getAttribute("src")).slice(0, 80)})`);
    }
  }

  return { found, counts, viewport: [vw, vh], page: [pageWidth, doc.scrollHeight], url: location.href };
})()
"""

KIND_ORDER = ["sideways scroll", "covered", "overlap", "off the page", "cut off", "text spills out",
              "no size", "broken image", "low contrast"]

LOOK_SYSTEM = ("You describe screenshots of web pages for a coding agent that cannot see them. Answer the "
               "question about this screenshot directly and concretely: name visible text, positions, colours "
               "and anything that looks broken. Do not guess at what is not visible. Keep it under 150 words.")


def screenshot(page: Page, snapshotter: Snapshotter | None = None, boxes: bool = False) -> Image:
    world = snapshotter.world if snapshotter is not None else None
    drew = False
    if boxes and world is not None:
        try:
            page.evaluate(BOXES_JS, context_id=world)
            drew = True
        except CDPError:
            pass
    try:
        shot = page.call("Page.captureScreenshot", {"format": "jpeg", "quality": JPEG_QUALITY}, timeout=20)
    finally:
        if drew:
            try:
                page.evaluate(REMOVE_BOXES_JS, context_id=world)
            except CDPError:
                pass
    return Image("image/jpeg", shot["data"])


def layout_check(page: Page, snapshotter: Snapshotter | None = None) -> str:
    world = snapshotter.world if snapshotter is not None else None
    script = LAYOUT_JS.replace("__MAX_PER_KIND__", str(MAX_PER_KIND))
    try:
        report = page.evaluate(script, context_id=world)
    except CDPError:
        report = page.evaluate(script)
    return format_layout(report)


def format_layout(report: dict) -> str:
    width, height = report["viewport"]
    page_width, page_height = report["page"]
    head = f"Layout check of {report['url'][:120]} (window {width}x{height}, page {page_width}x{page_height}):"
    found, counts = report["found"], report["counts"]
    if not found:
        return (head + "\nNothing wrong found: no sideways scrolling, overlaps, covered controls, cut-off or "
                "spilling text, broken images or low-contrast text.")
    lines = [head]
    for kind in KIND_ORDER + sorted(set(found) - set(KIND_ORDER)):
        items = found.get(kind)
        if not items:
            continue
        extra = counts[kind] - len(items)
        lines.append(f"{kind} ({counts[kind]}):" if extra <= 0 else f"{kind} ({counts[kind]}, first {len(items)} shown):")
        lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def helper_model() -> str | None:
    key = os.environ.get(HELPER_ENV, "").strip()
    if not key or key not in MODEL_REGISTRY or not has_vision(key):
        return None
    if not os.environ.get(MODEL_REGISTRY[key]["env_key"]):
        return None
    return key


def look(page: Page, question: str, helper: str, *, task_id: str | None = None, event_sink=None) -> str:
    image = screenshot(page)
    turn = Turn(role="user", text=f"Question: {question}", images=[image])
    response = timed_generate(get_provider(helper), [turn], [], LOOK_SYSTEM, model_key=helper,
                              task_id=task_id, event_sink=event_sink)
    return (response.text or "(the helper model gave no answer)").strip()
