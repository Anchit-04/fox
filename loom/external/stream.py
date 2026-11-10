import time
from pathlib import Path
from typing import Callable

from sandbox import sandbox_client

POLL_SECONDS = 0.5
FINISHED = ("exited", "stopped")
SHELL_SPECIALS = set(' \t&|<>^()%!,;=')


class ExternalError(Exception):
    pass


def quote(arg: str) -> str:
    if arg and not any(c in SHELL_SPECIALS or c == '"' for c in arg):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def shell_line(args: list[str], stdin: Path, stderr: Path) -> str:
    inner = " ".join(quote(a) for a in args) + f' < "{stdin}" 2> "{stderr}"'
    return f'"{inner}"'


def follow(command: str, on_line: Callable[[str], None], poll_seconds: float = POLL_SECONDS) -> int:
    reply = sandbox_client.start(command)
    process = reply.get("process")
    if not process:
        raise ExternalError(reply.get("output") or "could not start the process")

    pending = ""
    while True:
        pending += reply.get("output", "")
        *lines, pending = pending.split("\n")
        for line in lines:
            if line.strip():
                on_line(line.strip())
        if reply.get("status") in FINISHED:
            break
        time.sleep(poll_seconds)
        reply = sandbox_client.read(process)
        if reply.get("process") != process:
            raise ExternalError(reply.get("output") or f"lost track of {process}")

    if pending.strip():
        on_line(pending.strip())
    return reply.get("exit_code", -1)
