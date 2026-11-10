import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from events import emit
from external import stream
from external.cli import find_cli

STDERR_TAIL_CHARS = 1500
RESULT_CHARS = 2000


@dataclass
class Outcome:
    ok: bool
    final_text: str
    failure: str | None
    session_id: str | None


class Translator:
    model_key = ""

    def __init__(self, task_id: str | None, event_sink):
        self.task_id = task_id
        self.event_sink = event_sink
        self.final_text = ""
        self.failure: str | None = None
        self.finished = False
        self.session_id: str | None = None
        self.noise: list[str] = []
        self.started = time.monotonic()

    def elapsed_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)

    def feed(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            self.noise.append(line)
            return
        if isinstance(event, dict):
            self.handle(event)
        else:
            self.noise.append(line)

    def handle(self, event: dict) -> None:
        raise NotImplementedError

    def emit(self, event_type: str, **payload) -> None:
        emit(self.event_sink, event_type, {"task_id": self.task_id, **payload})

    def say(self, text: str) -> None:
        if text and text.strip():
            self.emit("agent_turn", model_key=self.model_key, text=text)

    def tool(self, name: str, args: dict, content: str) -> None:
        self.emit("tool_call", name=name, args=args)
        self.emit("tool_result", name=name, content=content)

    def wrote(self, path: str | None, created: bool, ok: bool, error: str = "") -> None:
        result = {"success": ok}
        if error:
            result["error"] = error[:RESULT_CHARS]
        self.tool("write_file" if created else "edit_file", {"path": path or ""}, json.dumps(result))

    def called_model(self, *, duration_ms: int, input_tokens: int, output_tokens: int, cache_read_tokens: int,
                     cache_write_tokens: int, cost: float | None, cost_source: str | None) -> None:
        self.emit("model_called", model_key=self.model_key, duration_ms=duration_ms, input_tokens=input_tokens,
                  output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
                  cache_write_tokens=cache_write_tokens, cost=cost, cost_source=cost_source, prefix=None)

    def outcome(self, exit_code: int, stderr: str) -> Outcome:
        failure = self.failure
        if failure is None and not self.finished:
            failure = f"{self.model_key} stopped without finishing (exit code {exit_code})"
        elif failure is None and exit_code != 0:
            failure = f"{self.model_key} finished but exited with code {exit_code}"
        if failure is not None:
            extra = (stderr.strip() or "\n".join(self.noise))[-STDERR_TAIL_CHARS:]
            if extra:
                failure += "\n" + extra
        return Outcome(ok=failure is None, final_text=self.final_text, failure=failure, session_id=self.session_id)


def run(worker, brief: str, mode: str, *, task_id: str | None, event_sink, resume: str | None = None) -> Outcome:
    cli = find_cli(worker.CLI)
    if cli is None:
        raise stream.ExternalError(f"{worker.NAME} isn't installed: no `{worker.CLI}` found on PATH or in npm's folder")
    folder = Path(tempfile.mkdtemp(prefix=f"fox-{worker.NAME}-"))
    try:
        stdin = folder / "brief.md"
        stdin.write_text(brief, encoding="utf-8")
        stderr = folder / "stderr.txt"
        translator = worker.Translator(task_id, event_sink)
        command = stream.shell_line(worker.command(cli, mode, resume), stdin, stderr)
        exit_code = stream.follow(command, translator.feed)
        errors = stderr.read_text(encoding="utf-8", errors="replace") if stderr.exists() else ""
        return translator.outcome(exit_code, errors)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
