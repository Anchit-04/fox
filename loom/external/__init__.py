from external import claude_code, codex, runner
from external.cli import find_cli
from external.runner import Outcome
from external.stream import ExternalError

WORKERS = {claude_code.NAME: claude_code, codex.NAME: codex}


def is_external(name: str | None) -> bool:
    return name in WORKERS


def available(name: str) -> bool:
    return is_external(name) and find_cli(WORKERS[name].CLI) is not None


def installed() -> list[str]:
    return [name for name in WORKERS if available(name)]


def brief(description: str, scope: list[str]) -> str:
    parts = [description.strip()]
    if scope:
        parts.append(f"Files this task is expected to change: {', '.join(scope)}. Leave other files alone; "
                     "if more need changing, say so in your summary instead of changing them.")
    parts.append("When you are done, finish with a short summary: what you did, which files you changed, "
                 "and how you checked it. If something stopped you, say what.")
    return "\n\n".join(parts)


def run(name: str, text: str, mode: str, *, task_id: str | None, event_sink, resume: str | None = None) -> Outcome:
    return runner.run(WORKERS[name], text, mode, task_id=task_id, event_sink=event_sink, resume=resume)
