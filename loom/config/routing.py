import external
from providers.config import MODEL_REGISTRY
from . import roster as rosters
from . import vault


class RoutingError(Exception):
    """No model satisfies the requested role, given what's currently in the vault."""


def _unusable(name: str) -> str | None:
    if external.is_external(name):
        return None if external.available(name) else "isn't installed on this machine"
    if name not in MODEL_REGISTRY:
        return "isn't a known model"
    if not vault.is_present(name):
        return "has no API key in the vault"
    return None


def _require_worker(name: str, roster: rosters.Roster, label: str) -> str:
    reason = _unusable(name)
    if reason is None and name in roster.off:
        reason = "is turned off as a worker in roster.json"
    if reason is not None:
        usable = ", ".join(rosters.workers(roster)) or "none"
        raise RoutingError(f"{label} {name!r} {reason}. Workers you can use: {usable}")
    return name


def pick_executor(preferred: str | None = None, fallback: str | None = None) -> str:
    roster = rosters.load()
    if preferred is not None:
        return _require_worker(preferred, roster, "requested worker")
    if roster.default_worker is not None:
        return _require_worker(roster.default_worker, roster, "default_worker in roster.json")
    names = rosters.workers(roster)
    if fallback in names:
        return fallback
    if not names:
        raise RoutingError("No worker is available: add a model key with ctrl+k, or install Claude Code or Codex.")
    raise RoutingError(f"No default worker: {fallback!r} can't be one. Name a worker in delegate_task, or set "
                       f"default_worker in roster.json. Workers you can use: {', '.join(names)}")


def default_worker(fallback: str | None = None) -> str | None:
    try:
        return pick_executor(fallback=fallback)
    except RoutingError:
        return None


def pick_chat() -> str:
    roster = rosters.load()
    if roster.default_chat is not None:
        name = roster.default_chat
        reason = _unusable(name)
        if reason is None and external.is_external(name):
            reason = "is an external CLI, and a chat can't run on one yet"
        if reason is not None:
            raise RoutingError(f"default_chat {name!r} in roster.json {reason}")
        return name
    raise RoutingError("No model chosen for this chat: pick one with --model or the model picker, or set "
                       "default_chat in roster.json.")
