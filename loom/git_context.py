import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import transport

QUIET_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": ":",
    "GIT_SEQUENCE_EDITOR": ":",
    "GIT_MERGE_AUTOEDIT": "no",
    "GIT_PAGER": "cat",
    "GCM_INTERACTIVE": "never",
    "GH_PROMPT_DISABLED": "1",
    "GH_PAGER": "",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_SPINNER_DISABLED": "1",
    "PAGER": "",
}

PROBE_TIMEOUT_SECONDS = 8

GIT_NOTE = """
## Git and GitHub
- Commit, push, open a pull request or merge only when the user asks. If they ask you to commit while on the default
  branch, make a new branch first unless they say otherwise.
- Before a commit, run `git status` and `git diff` and stage the files you mean by name. Write the message with -m;
  use several -m flags for a body. Don't amend, rebase or rewrite history unless asked.
- Commands here can't answer prompts: editors and pagers are switched off, so never use -i, `add -p`, or anything
  that waits for input. If git says an editor or a password is needed, tell the user instead of retrying.
- Never use --no-verify, --force pushes, `reset --hard`, `clean -f`, `branch -D`, or discard uncommitted work unless
  the user asked for exactly that. Fox asks the user before each of these, every time. If a hook fails, fix the cause
  and commit again.
- Use `gh` for GitHub: `gh pr create --title "..." --body "..."`, `gh pr view`, `gh pr diff`, `gh pr checks`,
  `gh run list`, `gh run view <id> --log-failed` to see why CI failed, `gh issue view <n>`, and `gh api` for anything
  else. Reading GitHub runs without asking; creating, merging, closing or commenting asks the user.
- After opening a pull request, give the user its URL. After a push, check CI with `gh pr checks` when they want it.
"""

_cache: dict[str, str] = {}
_lock = threading.Lock()


def quiet_env() -> dict:
    return {**os.environ, **QUIET_ENV}


def _run(args: list[str], workspace: Path) -> str | None:
    try:
        done = subprocess.run(args, cwd=workspace, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=PROBE_TIMEOUT_SECONDS, env=quiet_env(), **transport.hidden())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def _gh_account(workspace: Path) -> str:
    if shutil.which("gh") is None:
        return "`gh` is not installed, so GitHub is reachable only through git itself."
    try:
        done = subprocess.run(["gh", "auth", "status"], cwd=workspace, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT_SECONDS, env=quiet_env(),
                              **transport.hidden())
    except (OSError, subprocess.TimeoutExpired):
        return "`gh` is installed but didn't answer, so its sign-in is unknown."
    found = re.search(r"Logged in to (\S+) account (\S+)", done.stdout + done.stderr)
    if found:
        return f"`gh` is signed in to {found.group(1)} as {found.group(2)}."
    return "`gh` is installed but not signed in. If GitHub work is needed, ask the user to run `gh auth login`."


def snapshot(workspace: str | Path) -> str:
    workspace = Path(workspace).resolve()
    if _run(["git", "rev-parse", "--is-inside-work-tree"], workspace) != "true":
        return "- The workspace is not a git repository."
    lines = []
    top = _run(["git", "rev-parse", "--show-toplevel"], workspace)
    if top:
        lines.append(f"- The workspace is in the git repository at {top}.")
    remotes = _run(["git", "remote", "-v"], workspace)
    fetch = sorted({" ".join(line.split()[:2]) for line in (remotes or "").splitlines() if line.endswith("(fetch)")})
    lines.append("- Remotes: " + "; ".join(fetch) + "." if fetch else "- The repository has no remotes.")
    head = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], workspace)
    if head:
        lines.append(f"- The default branch is {head.split('/', 1)[-1]}.")
    lines.append("- " + _gh_account(workspace))
    lines.append("- The current branch and uncommitted changes move during the chat; run `git status` to see them.")
    return "\n".join(lines)


def prompt_note(workspace: str | Path) -> str:
    key = str(Path(workspace).resolve())
    with _lock:
        if key not in _cache:
            _cache[key] = GIT_NOTE + "\nThis workspace:\n" + snapshot(key) + "\n"
        return _cache[key]


def forget() -> None:
    with _lock:
        _cache.clear()
