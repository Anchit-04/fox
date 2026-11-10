import json
from dataclasses import dataclass, field
from pathlib import Path

import external
from paths import FOX_HOME
from providers.config import MODEL_REGISTRY, has_vision, price_text
from . import vault

ROSTER_FILE = FOX_HOME / "roster.json"


@dataclass(frozen=True)
class Roster:
    default_worker: str | None = None
    default_chat: str | None = None
    notes: dict[str, str] = field(default_factory=dict)
    off: frozenset[str] = frozenset()
    problems: tuple[str, ...] = ()


def known(name: str) -> bool:
    return name in MODEL_REGISTRY or external.is_external(name)


def _text(data: dict, key: str, problems: list[str]) -> str | None:
    value = data.get(key)
    if value is None or isinstance(value, str) and value.strip():
        return value.strip() if value else None
    problems.append(f"{key} must be a model name")
    return None


def load(path: Path | None = None) -> Roster:
    path = path or ROSTER_FILE
    if not path.exists():
        return Roster()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Roster(problems=(f"{path.name} was ignored: {e}",))
    if not isinstance(data, dict):
        return Roster(problems=(f"{path.name} was ignored: it must be a JSON object",))

    problems: list[str] = []
    default_worker = _text(data, "default_worker", problems)
    default_chat = _text(data, "default_chat", problems)
    notes = data.get("notes") or {}
    if not isinstance(notes, dict) or not all(isinstance(v, str) for v in notes.values()):
        problems.append("notes must map model names to text")
        notes = {}
    off = data.get("off") or []
    if not isinstance(off, list) or not all(isinstance(v, str) for v in off):
        problems.append("off must be a list of model names")
        off = []

    named = [n for n in (default_worker, default_chat) if n] + list(notes) + list(off)
    unknown = sorted({n for n in named if not known(n)})
    if unknown:
        problems.append(f"unknown names: {', '.join(unknown)}")
    return Roster(default_worker=default_worker, default_chat=default_chat,
                  notes={k: v.strip() for k, v in notes.items() if v.strip()},
                  off=frozenset(off), problems=tuple(problems))


def workers(roster: Roster) -> list[str]:
    names = [key for key in MODEL_REGISTRY if vault.is_present(key)] + external.installed()
    return [name for name in names if name not in roster.off]


def _describe(name: str) -> str:
    if external.is_external(name):
        return f"external CLI: {external.WORKERS[name].TITLE}, on the user's own account"
    parts = [price_text(name)]
    if has_vision(name):
        parts.insert(0, "sees images")
    return " · ".join(parts)


def render_for_prompt(roster: Roster, default: str | None) -> str:
    names = workers(roster)
    lines = ["\n## Workers you can delegate to"]
    if not names:
        lines.append("None are available: there is no model key in the vault and no external CLI installed, "
                     "so do the work yourself.")
    else:
        if default:
            lines.append(f"Pass one of these names as model_key in delegate_task. Leave model_key out to use "
                         f"the default worker, {default}.")
        else:
            lines.append("Pass one of these names as model_key in delegate_task.")
        for name in names:
            line = f"- {name}: {_describe(name)}"
            if name == default:
                line += " · default"
            if name in roster.notes:
                line += f" · the user's note: {roster.notes[name]}"
            lines.append(line)
        if any(external.is_external(n) for n in names):
            lines.append("External CLIs use their own tools inside this session's permission mode. They can't "
                         "call ask_orchestrator or ask the user anything; their final summary becomes the "
                         "task report.")
    if roster.problems:
        lines.append(f"(roster.json: {'; '.join(roster.problems)}. Tell the user if it matters.)")
    return "\n".join(lines) + "\n"
