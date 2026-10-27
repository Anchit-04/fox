"""
Shared memory: notes and task results every agent in a session can read.
Stored in fox.db as memory_written events; one Memory per session.
"""

import json
import threading
import uuid

import paths
from store import Store


class Memory:
    def __init__(self, store: Store, session_id: str):
        self.store = store
        self.session_id = session_id
        self._lock = threading.Lock()

    @classmethod
    def scratch(cls) -> "Memory":
        store = Store.open(":memory:")
        session_id = str(uuid.uuid4())
        store.append_event(session_id, "session_started", {
            "task": "(scratch)", "workspace": str(paths.workspace()), "mode": "agent",
            "permission_mode": "workspace-write", "model_key": None, "parent_id": None,
        })
        return cls(store, session_id)

    def append_entry(self, entry_id: str, agent: str, content: str, scope: list[str] | None = None,
                     kind: str = "note", task_id: str | None = None) -> None:
        self.store.append_event(self.session_id, "memory_written", {
            "task_id": task_id, "entry_id": entry_id, "agent": agent, "kind": kind,
            "scope": list(scope or []), "content": content,
        })

    def add_note(self, agent: str, content: str) -> str:
        with self._lock:
            entry_id = self.store.next_note_id(self.session_id)
            self.append_entry(entry_id, agent, content)
        return entry_id

    def read_index(self) -> str:
        lines = [_header(entry) for entry in self.store.memory_entries(self.session_id)]
        return "\n".join(lines) if lines else "(empty)"

    def read_entry(self, entry_id: str) -> str:
        entry = self.store.memory_entry(self.session_id, entry_id)
        if entry is None:
            return f"No entry with id={entry_id!r} found."
        return f"{_header(entry)}\n{entry['content']}"


def _header(entry: dict) -> str:
    scope = json.loads(entry["scope"])
    line = f"[{entry['created_at']}] id={entry['entry_id']} agent={entry['agent']} kind={entry['kind']}"
    return f"{line} scope={','.join(scope)}" if scope else line


_default: Memory | None = None


def default_memory() -> Memory:
    """Fallback for callers that didn't bring their own, mostly the standalone
    CLI. Kept in memory only: it is gone when the process ends."""
    global _default
    if _default is None:
        _default = Memory.scratch()
    return _default


MEMORY_WRITE_TOOL = {
    "type": "function",
    "name": "memory_write",
    "description": "Leave a note in shared memory for other agents to read. Task results are logged automatically; use this for extra context.",
    "parameters": {
        "type": "object",
        "properties": {"content": {"type": "string", "description": "The note."}},
        "required": ["content"],
    },
}

MEMORY_READ_TOOL = {
    "type": "function",
    "name": "memory_read",
    "description": "Read a full shared memory entry by id (ids are shown in the index in your system prompt).",
    "parameters": {
        "type": "object",
        "properties": {"entry_id": {"type": "string", "description": "Entry id, exactly as shown in the index."}},
        "required": ["entry_id"],
    },
}

MEMORY_TOOLS = [MEMORY_WRITE_TOOL, MEMORY_READ_TOOL]


def _memory_write(mem: Memory, agent: str, args: dict) -> str:
    return json.dumps({"ok": True, "id": mem.add_note(agent, args["content"])})


MEMORY_TOOL_HANDLERS = {
    "memory_write": _memory_write,
    "memory_read": lambda mem, agent, args: mem.read_entry(args["entry_id"]),
}
