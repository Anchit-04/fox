import base64
import hashlib
from typing import Callable

from providers import Image, ToolCall, ToolResult, Turn

BYTES = "__bytes__"
TURN = "history_turn"
RESULT = "history_result"
COMPACTED = "history_compacted"
HISTORY_EVENTS = (TURN, RESULT, COMPACTED)
INTERRUPTED_RESULT = ("Interrupted: Fox stopped before this call finished, so it may or may not have run. "
                      "Check the current state before retrying it.")

BlobPut = Callable[[Image], str]
BlobGet = Callable[[str], Image]


def blob_id(image: Image) -> str:
    return hashlib.sha256(base64.b64decode(image.data)).hexdigest()


def encode(value):
    if isinstance(value, (bytes, bytearray)):
        return {BYTES: base64.b64encode(bytes(value)).decode("ascii")}
    if hasattr(value, "model_dump"):
        return encode(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return {str(key): encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) == {BYTES}:
            return base64.b64decode(value[BYTES])
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def serialize_call(call: ToolCall) -> dict:
    data = {"id": call.id, "name": call.name, "args": encode(call.args)}
    if call.raw is not None:
        data["raw"] = encode(call.raw)
    return data


def deserialize_call(data: dict) -> ToolCall:
    return ToolCall(id=data["id"], name=data["name"], args=decode(data.get("args") or {}),
                    raw=decode(data["raw"]) if "raw" in data else None)


def serialize_result(result: ToolResult, put_blob: BlobPut) -> dict:
    data = {"call_id": result.call_id, "name": result.name, "content": result.content}
    if result.images:
        data["images"] = [put_blob(image) for image in result.images]
    return data


def deserialize_result(data: dict, get_blob: BlobGet) -> ToolResult:
    return ToolResult(call_id=data["call_id"], name=data["name"], content=data["content"],
                      images=[get_blob(blob) for blob in data.get("images", [])])


def serialize_turn(turn: Turn, put_blob: BlobPut) -> dict:
    data: dict = {"role": turn.role}
    if turn.text is not None:
        data["text"] = turn.text
    if turn.tool_calls:
        data["tool_calls"] = [serialize_call(call) for call in turn.tool_calls]
    if turn.tool_results:
        data["tool_results"] = [serialize_result(result, put_blob) for result in turn.tool_results]
    if turn.raw is not None:
        data["raw"] = encode(turn.raw)
    if turn.images:
        data["images"] = [put_blob(image) for image in turn.images]
    return data


def deserialize_turn(data: dict, get_blob: BlobGet) -> Turn:
    return Turn(
        role=data["role"],
        text=data.get("text"),
        tool_calls=[deserialize_call(call) for call in data.get("tool_calls", [])],
        tool_results=[deserialize_result(result, get_blob) for result in data.get("tool_results", [])],
        raw=decode(data["raw"]) if "raw" in data else None,
        images=[get_blob(blob) for blob in data.get("images", [])],
    )


class HistoryRecorder:
    def __init__(self, store, session_id: str, task_id: str | None = None):
        self.store = store
        self.session_id = session_id
        self.task_id = task_id
        events = store.history_events(session_id, task_id)
        self.parent = events[-1]["seq"] if events else None

    def turn(self, turn: Turn, **meta) -> int:
        payload = {"turn": serialize_turn(turn, self.store.put_blob)}
        if meta:
            payload["meta"] = meta
        return self._append(TURN, payload)

    def result(self, result: ToolResult) -> int:
        return self._append(RESULT, {"result": serialize_result(result, self.store.put_blob)})

    def compacted(self, summary: str) -> int:
        return self._append(COMPACTED, {"summary": summary})

    def _append(self, kind: str, payload: dict) -> int:
        seq = self.store.append_event(self.session_id, kind,
                                      {"task_id": self.task_id, "parent": self.parent, **payload})
        self.parent = seq
        return seq


def fold(events: list[dict], get_blob: BlobGet) -> list[Turn]:
    turns: list[Turn] = []
    for event in events:
        kind, payload = event["type"], event["payload"]
        if kind == COMPACTED:
            turns = [Turn(role="user", text=payload["summary"])]
        elif kind == TURN:
            turns.append(deserialize_turn(payload["turn"], get_blob))
        elif kind == RESULT:
            result = deserialize_result(payload["result"], get_blob)
            if turns and _open_results(turns[-1]):
                turns[-1].tool_results.append(result)
            else:
                turns.append(Turn(role="user", tool_results=[result]))
    return turns


def _open_results(turn: Turn) -> bool:
    return turn.role == "user" and bool(turn.tool_results) and turn.text is None


def repair_tail(turns: list[Turn]) -> list[Turn]:
    for index in range(len(turns) - 1, -1, -1):
        if turns[index].role == "assistant" and turns[index].tool_calls:
            break
    else:
        return turns
    asked = turns[index]
    following = turns[index + 1] if index + 1 < len(turns) else None
    if following is not None and not _open_results(following):
        return turns
    answered = {result.call_id for result in following.tool_results} if following else set()
    missing = [ToolResult(call_id=call.id, name=call.name, content=INTERRUPTED_RESULT)
               for call in asked.tool_calls if call.id not in answered]
    if not missing:
        return turns
    if following is None:
        turns.append(Turn(role="user", tool_results=missing))
    else:
        following.tool_results.extend(missing)
    return turns


def rebuild_history(store, session_id: str, task_id: str | None = None) -> list[Turn]:
    return repair_tail(fold(store.history_events(session_id, task_id), store.get_blob))


def last_meta(events: list[dict], key: str, default=None):
    for event in reversed(events):
        meta = event["payload"].get("meta") or {}
        if key in meta:
            return meta[key]
    return default


class Journal:
    def __init__(self, store, session_id: str):
        self.store = store
        self.session_id = session_id

    def recorder(self, task_id: str | None = None) -> HistoryRecorder:
        return HistoryRecorder(self.store, self.session_id, task_id)

    def turns(self, task_id: str | None = None) -> list[Turn]:
        return rebuild_history(self.store, self.session_id, task_id)

    def meta(self, key: str, task_id: str | None = None, default=None):
        return last_meta(self.store.history_events(self.session_id, task_id), key, default)

    def agents(self) -> list[str | None]:
        return self.store.history_agents(self.session_id)
