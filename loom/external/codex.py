import json
import os
import tomllib
from pathlib import Path

import permissions
from external import runner

NAME = "codex"
CLI = "codex"
TITLE = "Codex, OpenAI's coding agent"

SANDBOX_MODES = {
    permissions.READ_ONLY: "read-only",
    permissions.WORKSPACE_WRITE: "workspace-write",
    permissions.FULL_ACCESS: "danger-full-access",
}


def _config_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    return Path(home) / "config.toml" if home else Path.home() / ".codex" / "config.toml"


def _windows_sandbox_chosen() -> bool:
    try:
        config = tomllib.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    windows = config.get("windows")
    return isinstance(windows, dict) and "sandbox" in windows


def command(cli: str, mode: str, resume: str | None) -> list[str]:
    args = [cli, "exec"] + (["resume"] if resume else [])
    args += ["--json", "--skip-git-repo-check", "-c", f"sandbox_mode={SANDBOX_MODES[mode]}"]
    if os.name == "nt" and not _windows_sandbox_chosen():
        args += ["-c", "windows.sandbox=unelevated"]
    if resume:
        args.append(resume)
    return args + ["-"]


class Translator(runner.Translator):
    model_key = NAME

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "thread.started":
            self.session_id = event.get("thread_id") or self.session_id
        elif kind == "item.completed":
            self._item(event.get("item") or {})
        elif kind == "turn.completed":
            self.finished = True
            usage = event.get("usage") or {}
            cached = usage.get("cached_input_tokens") or 0
            self.called_model(duration_ms=self.elapsed_ms(), input_tokens=(usage.get("input_tokens") or 0) - cached,
                              output_tokens=usage.get("output_tokens") or 0, cache_read_tokens=cached,
                              cache_write_tokens=usage.get("cache_write_input_tokens") or 0,
                              cost=None, cost_source=None)
        elif kind == "turn.failed":
            self.finished = True
            self.failure = (event.get("error") or {}).get("message") or "Codex's turn failed"
        elif kind == "error":
            self.failure = event.get("message") or "Codex reported an error"

    def _item(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "agent_message":
            self.say(item.get("text", ""))
            self.final_text = item.get("text", "")
        elif kind == "file_change":
            ok = item.get("status") == "completed"
            for change in item.get("changes") or []:
                self.wrote(change.get("path"), change.get("kind") == "add", ok)
        elif kind == "command_execution":
            output = (item.get("aggregated_output") or "")[-runner.RESULT_CHARS:]
            self.tool("shell", {"command": item.get("command", "")},
                      json.dumps({"exit_code": item.get("exit_code"), "output": output}))
        elif kind == "mcp_tool_call":
            self.tool(f"{item.get('server', 'mcp')}.{item.get('tool', '')}", item.get("arguments") or {},
                      json.dumps(item.get("result") or item.get("error") or {})[:runner.RESULT_CHARS])
        elif kind == "web_search":
            self.tool("web_search", {"query": item.get("query", "")}, "")
