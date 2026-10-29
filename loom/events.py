from typing import Callable

EventSink = Callable[[str, dict], None]


def emit(event_sink: "EventSink | None", event_type: str, payload: dict) -> None:
    if event_sink is not None:
        event_sink(event_type, payload)


EVENTS: dict[str, frozenset[str]] = {
    "session_started": frozenset({"task", "workspace", "mode", "permission_mode", "model_key", "parent_id"}),
    "session_ended": frozenset({"status", "final_text"}),
    "agent_turn": frozenset({"task_id", "model_key", "text"}),
    "todo_updated": frozenset({"task_id", "checklist"}),
    "task_created": frozenset({"task_id", "description", "lane", "scope"}),
    "task_status_changed": frozenset({"task_id", "status", "error", "report", "model_key"}),
    "memory_written": frozenset({"task_id", "entry_id", "agent", "kind", "scope", "content"}),
    "tool_call": frozenset({"task_id", "name", "args"}),
    "tool_result": frozenset({"task_id", "name", "content"}),
    "tool_denied": frozenset({"task_id", "name", "reason"}),
    "task_results_delivered": frozenset({"task_id", "content"}),
    "proposal_made": frozenset({"task_id", "text"}),
    "scope_audit": frozenset({"task_id", "lane", "declared", "written", "undeclared", "unused", "mispredicted"}),
    "human_message_injected": frozenset({"task_id", "content"}),
    "approval_requested": frozenset({"request_id", "tool_name", "args", "reason", "description"}),
    "error": frozenset({"task_id", "message"}),
}
