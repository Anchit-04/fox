import threading

import paths
import permissions
from events import emit
from tools import file_tools

EVENT = "file_baseline"
MAX_BYTES = 1_000_000

_seen: set = set()
_lock = threading.Lock()


def label(target) -> str:
    try:
        return target.relative_to(paths.workspace().resolve()).as_posix()
    except ValueError:
        return str(target)


def read(target) -> tuple[bool, str | None]:
    try:
        if target.stat().st_size > MAX_BYTES:
            return True, None
        data = target.read_bytes()
    except FileNotFoundError:
        return False, ""
    except OSError:
        return True, None
    if b"\0" in data:
        return True, None
    try:
        return True, data.decode("utf-8")
    except UnicodeDecodeError:
        return True, None


def capture(event_sink, task_id, tool_name: str, args: dict) -> None:
    if event_sink is None or tool_name not in permissions.WRITE_TOOLS:
        return
    try:
        target = file_tools._resolve(args.get("path") or "")
    except Exception:
        return
    path = label(target)
    with _lock:
        if (event_sink, path) in _seen:
            return
        _seen.add((event_sink, path))
    existed, content = read(target)
    emit(event_sink, EVENT, {"task_id": task_id, "path": path, "existed": existed, "content": content})
