from typing import Callable

# event_sink is None by default (no server attached) — purely additive, the
# single-agent CLI is unaffected.
EventSink = Callable[[str, dict], None]


def emit(event_sink: "EventSink | None", event_type: str, payload: dict) -> None:
    if event_sink is not None:
        event_sink(event_type, payload)
