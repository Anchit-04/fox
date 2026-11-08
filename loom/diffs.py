import difflib
import subprocess
from pathlib import Path

import transport
from baselines import read

MAX_LINES = 3000
MAX_UNTRACKED = 200
GIT_TIMEOUT_SECONDS = 15
NOT_SHOWN = "not shown: binary, unreadable, or over 1 MB"


def _counts(lines: list[str]) -> tuple[int, int]:
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return added, removed


def _entry(path: str, status: str, lines: list[str], note: str = "") -> dict:
    added, removed = _counts(lines)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        note = note or f"cut at {MAX_LINES} lines"
    return {"path": path, "status": status, "added": added, "removed": removed, "diff": "\n".join(lines),
            "note": note}


def unified(path: str, old: str, new: str) -> list[str]:
    before = "/dev/null" if old is None else f"a/{path}"
    after = "/dev/null" if new is None else f"b/{path}"
    return list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), before, after,
                                     lineterm=""))


def fox_changes(rows: list[dict], workspace: str | Path) -> tuple[list[dict], str]:
    workspace = Path(workspace)
    files = []
    for row in rows:
        path = row["path"]
        target = Path(path) if Path(path).is_absolute() else workspace / path
        exists, now = read(target)
        existed, before = bool(row["existed"]), row["content"]
        if not existed and not exists:
            continue
        status = "added" if not existed else "deleted" if not exists else "modified"
        if (existed and before is None) or (exists and now is None):
            files.append(_entry(path, status, [], NOT_SHOWN))
            continue
        if status == "modified" and before == now:
            continue
        if status == "modified" and before.splitlines() == now.splitlines():
            files.append(_entry(path, status, [], "only the line endings changed"))
            continue
        lines = unified(path, before if existed else None, now if exists else None)
        files.append(_entry(path, status, lines))
    files.sort(key=lambda f: f["path"])
    note = "" if files else "Fox hasn't changed any files in this chat yet."
    return files, note


def _git(workspace: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=workspace, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=GIT_TIMEOUT_SECONDS, **transport.hidden())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def split_git_diff(text: str) -> list[dict]:
    files, chunk = [], []
    for line in text.splitlines():
        if line.startswith("diff --git ") and chunk:
            files.append(_git_entry(chunk))
            chunk = []
        chunk.append(line)
    if chunk:
        files.append(_git_entry(chunk))
    return files


def _git_entry(chunk: list[str]) -> dict:
    header = chunk[0].split(" b/", 1)
    path = header[1] if len(header) == 2 else chunk[0]
    status = "modified"
    for line in chunk[1:6]:
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "deleted"
        elif line.startswith("rename to "):
            status, path = "renamed", line[len("rename to "):]
    body = [line for line in chunk if line.startswith(("---", "+++", "@@", "+", "-", " ", "\\"))]
    note = NOT_SHOWN if any(line.startswith("Binary files") for line in chunk) else ""
    return _entry(path, status, body, note)


def git_changes(workspace: str | Path) -> tuple[list[dict], str]:
    workspace = Path(workspace)
    if (_git(workspace, "rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return [], "This folder isn't a git repository, so there is no git diff. Press g for Fox's changes."
    text = _git(workspace, "diff", "HEAD", "--no-color", "--no-ext-diff")
    if text is None:
        text = _git(workspace, "diff", "--no-color", "--no-ext-diff") or ""
    files = split_git_diff(text)
    untracked = [p for p in (_git(workspace, "ls-files", "--others", "--exclude-standard", "-z") or "").split("\0") if p]
    for path in untracked[:MAX_UNTRACKED]:
        exists, now = read(workspace / path)
        if now is None:
            files.append(_entry(path, "untracked", [], NOT_SHOWN))
        else:
            files.append(_entry(path, "untracked", unified(path, None, now)))
    files.sort(key=lambda f: f["path"])
    note = "" if files else "No uncommitted changes."
    if len(untracked) > MAX_UNTRACKED:
        note = f"Showing {MAX_UNTRACKED} of {len(untracked)} untracked files."
    return files, note


def changes(source: str, rows: list[dict], workspace: str | Path) -> tuple[list[dict], str]:
    if source == "git":
        return git_changes(workspace)
    return fox_changes(rows, workspace)
