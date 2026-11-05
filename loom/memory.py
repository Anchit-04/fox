"""
Shared memory: notes and task results every agent in a session can read.
Stored in fox.db as memory_written events; one Memory per session.
"""

import json
import uuid
from pathlib import Path

import paths
from store import Store, project_space

INDEX_LIMIT = 20


class Memory:
    def __init__(self, store: Store, session_id: str, read: list[str] | None = None, write: str | None = None,
                 tag: str = ""):
        self.store = store
        self.session_id = session_id
        self.write_space = write or session_id
        self.read_spaces = list(read or [self.write_space])
        self.prefix = f"{tag}." if tag else ""

    @classmethod
    def for_chat(cls, store: Store, session_id: str, workspace: str | Path, shared: bool) -> "Memory":
        if not shared:
            return cls(store, session_id)
        space = project_space(workspace)
        return cls(store, session_id, read=[space], write=space, tag=store.space_tag(session_id, space))

    @classmethod
    def scratch(cls) -> "Memory":
        store = Store.open(":memory:")
        session_id = str(uuid.uuid4())
        store.append_event(session_id, "session_started", {
            "task": "(scratch)", "workspace": str(paths.workspace()), "mode": "agent",
            "permission_mode": "workspace-write", "model_key": None, "parent_id": None, "memory_space": session_id,
        })
        return cls(store, session_id)

    def append_entry(self, entry_id: str, agent: str, content: str, scope: list[str] | None = None,
                     kind: str = "note", task_id: str | None = None) -> None:
        self.store.append_event(self.session_id, "memory_written", {
            "task_id": task_id, "entry_id": self.prefix + entry_id, "space_id": self.write_space, "agent": agent,
            "kind": kind, "scope": list(scope or []), "content": content,
        })

    def add_note(self, agent: str, content: str) -> str:
        _, entry_id = self.store.append_note(self.session_id, self.write_space, self.prefix, {
            "task_id": None, "agent": agent, "scope": [], "content": content,
        })
        return entry_id

    def entries(self) -> list[dict]:
        return self.store.memory_entries(self.read_spaces)

    def read_index(self, limit: int | None = None) -> str:
        entries = self.entries()
        shown = entries if limit is None else entries[-limit:] if limit > 0 else []
        lines = [_header(entry) for entry in shown]
        if len(shown) < len(entries):
            lines.insert(0, f"({len(entries) - len(shown)} earlier entries not listed)")
        return "\n".join(lines) if lines else "(empty)"

    def notes(self, skip: int = 0) -> list[str]:
        entries = [entry for entry in self.entries() if entry["kind"] == "note"]
        return [_header(entry) for entry in entries[skip:]]

    def join(self, limit: int = INDEX_LIMIT) -> tuple[int, str]:
        entries = self.entries()
        notes = sum(1 for entry in entries if entry["kind"] == "note")
        if not entries:
            return notes, ""
        shown = entries[-limit:] if limit > 0 else []
        lines = [_header(entry) for entry in shown]
        head = (f"[Shared project memory: other chats in this project have written {len(entries)} "
                f"entr{'y' if len(entries) == 1 else 'ies'} before you joined. ")
        head += ("All of them are listed below" if len(shown) == len(entries)
                 else f"The latest {len(shown)} are listed below")
        head += "; memory_read shows one in full.]"
        return notes, "\n".join([head, *lines])

    def read_entry(self, entry_id: str) -> str:
        entry = self.store.memory_entry(self.read_spaces, entry_id)
        if entry is None and self.prefix and not entry_id.startswith(self.prefix):
            entry = self.store.memory_entry(self.read_spaces, self.prefix + entry_id)
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
    "description": "Read a full shared memory entry by id: a task id such as t3, or a note id such as note_2.",
    "parameters": {
        "type": "object",
        "properties": {"entry_id": {"type": "string", "description": "Entry id, exactly as shown."}},
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
