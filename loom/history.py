from events import EventSink, emit
from providers import ToolResult, Turn

IMAGE_DROPPED = "\n[An older screenshot was removed here to save context.]"


def open_results(turn: Turn) -> bool:
    return turn.role == "user" and bool(turn.tool_results) and turn.text is None


class History:
    def __init__(self, task: str | None = None, recorder=None, turns: list[Turn] | None = None, **meta):
        self.recorder = recorder
        self.turns: list[Turn] = []
        if turns is not None:
            self.turns = list(turns)
        else:
            self.add(Turn(role="user", text=task), **meta)

    def add(self, turn: Turn, **meta) -> None:
        self.turns.append(turn)
        if self.recorder is not None:
            self.recorder.turn(turn, **meta)

    def add_result(self, result: ToolResult) -> None:
        if self.turns and open_results(self.turns[-1]):
            self.turns[-1].tool_results.append(result)
        else:
            self.turns.append(Turn(role="user", tool_results=[result]))
        if self.recorder is not None:
            self.recorder.result(result)

    def add_human_messages(self, messages: list[str], task_id: str | None, event_sink: EventSink | None) -> None:
        for msg in messages:
            self.add(Turn(role="user", text=f"[Message from human]: {msg}"))
            emit(event_sink, "human_message_injected", {"task_id": task_id, "content": msg})

    def replace_with_summary(self, text: str) -> None:
        if self.recorder is not None:
            self.recorder.compacted(text)
        self.turns = [Turn(role="user", text=text)]

    def keep_latest_images(self, keep: int) -> None:
        kept = 0
        for turn in reversed(self.turns):
            for result in reversed(turn.tool_results):
                if not result.images:
                    continue
                if kept < keep:
                    kept += 1
                else:
                    result.images = []
                    result.content += IMAGE_DROPPED
