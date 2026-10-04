import json

import paths
from agent import _emit
from orchestration.tasks import Task


WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def normalize(path: str) -> str:
    root = paths.workspace()
    resolved = (root / path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def covers(entry: str, path: str) -> bool:
    return entry == "." or path == entry or path.startswith(entry + "/")


def _succeeded(content: str) -> bool:
    try:
        return json.loads(content).get("success") is True
    except (ValueError, TypeError, AttributeError):
        return False


class ScopeAudit:
    def __init__(self, task: Task, event_sink):
        self.task = task
        self._inner = event_sink
        self.declared = {normalize(p) for p in task.scope}
        self.written: set[str] = set()
        self._pending: str | None = None

    def sink(self, event_type: str, payload: dict) -> None:
        if payload.get("task_id") == self.task.id and payload.get("name") in WRITE_TOOLS:
            if event_type == "tool_call":
                path = (payload.get("args") or {}).get("path")
                self._pending = path if isinstance(path, str) and path.strip() else None
            elif event_type == "tool_result":
                if self._pending is not None and _succeeded(payload.get("content")):
                    self.written.add(normalize(self._pending))
                self._pending = None
        _emit(self._inner, event_type, payload)

    @property
    def undeclared(self) -> set[str]:
        return {p for p in self.written if not any(covers(d, p) for d in self.declared)}

    @property
    def unused(self) -> set[str]:
        return {d for d in self.declared if not any(covers(d, p) for p in self.written)}

    def finish(self) -> None:
        _emit(self._inner, "scope_audit", {
            "task_id": self.task.id,
            "lane": self.task.lane.value,
            "declared": sorted(self.declared),
            "written": sorted(self.written),
            "undeclared": sorted(self.undeclared),
            "unused": sorted(self.unused),
            "mispredicted": bool(self.undeclared),
        })
