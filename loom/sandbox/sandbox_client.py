"""
Python-side client for sandboxd (loom/sandbox/sandboxd/main.go) — the Go
binary that actually executes shell commands, with real OS-level
containment via a Windows Job Object (hard memory ceiling, guaranteed
process-tree kill on timeout).

Protocol: one JSON request per line into sandboxd's stdin, one JSON response
per line back on stdout. sandboxd is long-lived — we keep one instance
running for the whole session instead of spawning a process per command,
restarting it transparently if it dies.
"""

import atexit
import json
import queue
import subprocess
import threading
import uuid
from pathlib import Path

import git_context
import paths
import transport

MAX_OUTPUT_CHARS = 30_000  # context-budget cap, not a sandboxing concern — sandboxd itself doesn't truncate
COMMAND_TIMEOUT_SECONDS = 30
MAX_BACKGROUND_PROCESSES = 8

SHELL_NOTE = f"""Shell:
- run_bash_command runs each command in Windows cmd.exe (as `cmd /C <command>`). It is not bash and not PowerShell.
- Use cmd syntax: `dir`, `type file.txt`, `cd`, `where`, `set`, `findstr`, `%USERPROFILE%`, `&&` to chain commands.
- Don't use `ls`, `pwd`, `cat`, `grep`, `rm`, `touch` or `$HOME`: cmd doesn't have them. Use list_directory and search_files for files and code.
- For PowerShell, run `powershell -NoProfile -Command "..."`.
- Every command starts in the workspace folder. A `cd` does not carry over to the next command.
- Everything a command starts is closed when the command ends: `start`, `explorer`, browsers, and servers left
  running in the background. Exit code 0 from `start` does not mean anything opened. Use browser_open for web pages.
- A command is stopped after {COMMAND_TIMEOUT_SECONDS} seconds, and output past {MAX_OUTPUT_CHARS} characters is cut.
- To keep something running (a dev server, a file watcher, a recorder), use start_process instead. Check on it
  with read_process and end it with stop_process. Background processes are shared by this whole session,
  at most {MAX_BACKGROUND_PROCESSES} run at once, and all of them are stopped when the session ends.
"""

# Comfortably above sandboxd's own 30s per-command timeout (main.go's
# DefaultTimeout) — this should only fire if sandboxd itself is hung.
CLIENT_READ_TIMEOUT = COMMAND_TIMEOUT_SECONDS + 15

_SANDBOXD_PATH = Path(__file__).parent / "sandboxd" / "sandboxd.exe"

OUTPUT_ENCODING_ENV = {"PYTHONIOENCODING": "utf-8"}


def command_env() -> dict:
    env = git_context.quiet_env()
    for name, value in OUTPUT_ENCODING_ENV.items():
        env.setdefault(name, value)
    return env


def _start() -> subprocess.Popen:
    if not _SANDBOXD_PATH.exists():
        raise RuntimeError(
            f"sandboxd binary not found at {_SANDBOXD_PATH}. Build it first: "
            f"cd loom/sandbox/sandboxd && go build -o sandboxd.exe ."
        )
    proc = subprocess.Popen(
        [str(_SANDBOXD_PATH)],
        # Starting directory for every command, not a boundary — a command is
        # free to cd out of it. Read per call, since sandboxd is started lazily
        # and the workspace is chosen at startup.
        cwd=paths.workspace(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,  # line-buffered, matches the line-per-message protocol
        env=command_env(),
        **transport.hidden(),
    )
    return proc


def _stop(proc: subprocess.Popen) -> None:
    """Closing stdin asks sandboxd to exit cleanly (its EOF signal); wait()
    gives it a few seconds, kill() is the guaranteed backstop if it's stuck."""
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


class _Daemon:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.pending: dict[str, queue.Queue] = {}
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.dead: str | None = None
        threading.Thread(target=self._read, daemon=True, name="sandboxd-reader").start()

    def alive(self) -> bool:
        return self.dead is None and self.proc.poll() is None

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    self._fail(f"sandboxd sent a malformed response: {line!r}")
                    self.proc.kill()
                    return
                with self.lock:
                    waiter = self.pending.pop(resp.pop("id", ""), None)
                if waiter is not None:
                    waiter.put(resp)
        except (OSError, ValueError):
            pass
        self._fail("sandboxd process died unexpectedly")

    def _fail(self, reason: str) -> None:
        with self.lock:
            self.dead = self.dead or reason
            waiters, self.pending = self.pending, {}
        for waiter in waiters.values():
            waiter.put({"exit_code": -1, "output": reason})

    def call(self, request: dict, timeout: float) -> dict:
        request_id = uuid.uuid4().hex
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self.lock:
            if self.dead is not None:
                return {"exit_code": -1, "output": self.dead}
            self.pending[request_id] = waiter
        try:
            with self.write_lock:
                self.proc.stdin.write(json.dumps({**request, "id": request_id}) + "\n")
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            with self.lock:
                self.pending.pop(request_id, None)
            return {"exit_code": -1, "output": f"sandboxd pipe error: {e}"}
        try:
            return waiter.get(timeout=timeout)
        except queue.Empty:
            with self.lock:
                self.pending.pop(request_id, None)
            self._fail(f"sandboxd did not answer a command within {timeout}s and was restarted")
            self.proc.kill()
            return {"exit_code": -1, "output": f"sandboxd did not respond within {timeout}s "
                                               "— killed the hung process; it will restart on the next command."}


_daemon: _Daemon | None = None
_daemon_lock = threading.Lock()


def _cleanup_at_exit() -> None:
    if _daemon is not None:
        _stop(_daemon.proc)


atexit.register(_cleanup_at_exit)


def _get_daemon() -> _Daemon:
    global _daemon
    with _daemon_lock:
        if _daemon is None or not _daemon.alive():
            _daemon = _Daemon(_start())
        return _daemon


def _ask(request: dict) -> dict:
    try:
        daemon = _get_daemon()
    except RuntimeError as e:
        return {"exit_code": -1, "output": str(e)}
    return daemon.call(request, CLIENT_READ_TIMEOUT)


def run_bash_command(command: str) -> str:
    resp = _ask({"command": command})
    output = resp.get("output", "")
    if len(output) > MAX_OUTPUT_CHARS:
        resp["output"] = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(output)} chars total]"
    return json.dumps(resp)


def _keep_tail(resp: dict) -> str:
    output = resp.get("output", "")
    if len(output) > MAX_OUTPUT_CHARS:
        resp["output"] = f"[... {len(output) - MAX_OUTPUT_CHARS} earlier chars cut]\n" + output[-MAX_OUTPUT_CHARS:]
    return json.dumps(resp)


def start(command: str) -> dict:
    return _ask({"op": "start", "command": command})


def read(process: str = "") -> dict:
    return _ask({"op": "read", "process": process})


def stop(process: str) -> dict:
    return _ask({"op": "stop", "process": process})


def start_process(command: str) -> str:
    return _keep_tail(start(command))


def read_process(process: str = "") -> str:
    return _keep_tail(read(process))


def stop_process(process: str) -> str:
    return _keep_tail(stop(process))
