import permissions
from external import runner

NAME = "claude-code"
CLI = "claude"
TITLE = "Claude Code, Anthropic's coding agent"

PERMISSION_MODES = {
    permissions.READ_ONLY: "plan",
    permissions.WORKSPACE_WRITE: "acceptEdits",
    permissions.FULL_ACCESS: "bypassPermissions",
}

FILE_TOOLS = {"Write": True, "Edit": False, "MultiEdit": False, "NotebookEdit": False}


def command(cli: str, mode: str, resume: str | None) -> list[str]:
    args = [cli, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", PERMISSION_MODES[mode], "--permission-prompts", "none"]
    if resume:
        args += ["--resume", resume]
    return args


def _blocks(event: dict) -> list:
    content = (event.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _usage(usage: dict) -> dict:
    return {
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
        "cache_write_tokens": usage.get("cache_creation_input_tokens") or 0,
    }


class Translator(runner.Translator):
    model_key = NAME

    def __init__(self, task_id, event_sink):
        super().__init__(task_id, event_sink)
        self.calls: dict[str, tuple[str, dict]] = {}

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id") or self.session_id
        elif kind == "assistant":
            for block in _blocks(event):
                if block.get("type") == "text":
                    self.say(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    self.calls[block.get("id")] = (block.get("name", ""), block.get("input") or {})
        elif kind == "user":
            for block in _blocks(event):
                if block.get("type") == "tool_result":
                    self._finish_call(block)
        elif kind == "result":
            self._result(event)

    def _finish_call(self, block: dict) -> None:
        name, args = self.calls.pop(block.get("tool_use_id"), ("", {}))
        if not name:
            return
        text = _text(block.get("content"))
        failed = bool(block.get("is_error"))
        if name in FILE_TOOLS:
            path = args.get("file_path") or args.get("notebook_path")
            self.wrote(path, FILE_TOOLS[name], not failed, text if failed else "")
        else:
            self.tool(name, args, text[:runner.RESULT_CHARS])

    def _result(self, event: dict) -> None:
        self.finished = True
        self.session_id = event.get("session_id") or self.session_id
        text = event.get("result") or ""
        if event.get("is_error"):
            self.failure = text or f"Claude Code ended with {event.get('subtype') or 'an error'}"
        else:
            self.final_text = text
        self.called_model(duration_ms=event.get("duration_ms") or 0, cost=event.get("total_cost_usd"),
                          cost_source="provider", **_usage(event.get("usage") or {}))
