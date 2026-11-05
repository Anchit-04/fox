from events import EventSink, emit
from providers import Turn

IMAGE_DROPPED = "\n[An older screenshot was removed here to save context.]"


class History:
    def __init__(self, task: str):
        self.turns: list[Turn] = [Turn(role="user", text=task)]

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)

    def add_human_messages(self, messages: list[str], task_id: str | None, event_sink: EventSink | None) -> None:
        for msg in messages:
            self.add(Turn(role="user", text=f"[Message from human]: {msg}"))
            emit(event_sink, "human_message_injected", {"task_id": task_id, "content": msg})

    def replace_with_summary(self, text: str) -> None:
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
