"""
How an ASK decision actually reaches a human.

permissions.py decides *whether* to ask; this module decides *who to ask and
how*. Splitting them is what lets the same policy serve the plain CLI (which
can block on input()) and the WebSocket server (which cannot — input() from a
worker thread reads the server's own stdin, not the user's terminal).

Three approvers, one interface:

  CLIApprover   — python agent.py, a real TTY to prompt on
  DenyApprover  — no human reachable; refuse ASK rather than assume consent
  ApprovalGate  — server sessions: emit an event, block this thread, wake on
                  the reply. Same Condition pattern as InjectionQueue.

DenyApprover is the important default. The old code passed interactive=False
for every server session, which skipped the permission check entirely and let
the agent write files unasked — the bug that put new_folder/ in the repo root.
Failing closed turns that into a refusal the model can react to, instead of a
silent grant.
"""

import itertools
import threading

HANDOFF_ENDED = "ended"


class Approver:
    """Interface. approve() is called only for ASK decisions — ALLOW and DENY
    are settled by policy and never reach here."""

    def approve(self, tool_name: str, args: dict, reason: str, every_time: bool = False) -> bool:
        raise NotImplementedError

    def remember(self, tool_name: str) -> None:
        """Called after an 'always' answer. Default: no memory."""

    def start_handoff(self, *, kind: str, message: str, url: str = "", task_id: str | None = None) -> str | None:
        return None

    def handoff_answer(self, request_id: str) -> bool | str | None:
        return HANDOFF_ENDED

    def finish_handoff(self, request_id: str) -> None:
        pass


class DenyApprover(Approver):
    """Nobody to ask, so nothing is granted. Used for delegated executors,
    which run on worker threads with no route back to a human."""

    def approve(self, tool_name: str, args: dict, reason: str, every_time: bool = False) -> bool:
        return False


class CLIApprover(Approver):
    """Prompts on the terminal. 'always' is remembered per tool for the rest
    of the process — it applies to escalations of the same kind, not to a
    blanket grant, since policy already filtered out everything routine."""

    def __init__(self):
        self._always: set[str] = set()

    def remember(self, tool_name: str) -> None:
        self._always.add(tool_name)

    def approve(self, tool_name: str, args: dict, reason: str, every_time: bool = False) -> bool:
        if tool_name in self._always and not every_time:
            return True
        print(f"\n\033[91m[approve]\033[0m {describe(tool_name, args)}")
        print(f"\033[93m  why ask:\033[0m {reason}")
        try:
            answer = input("Allow? [y]es / [n]o / [a]lways: ").strip().lower()
        except EOFError:
            # No TTY (piped, CI, background). Fail closed — same reasoning as
            # DenyApprover: absence of a human is not consent.
            print("\033[91mNo input available — denying.\033[0m")
            return False
        if answer == "a":
            if not every_time:
                self.remember(tool_name)
            return True
        return answer == "y"

    def start_handoff(self, *, kind: str, message: str, url: str = "", task_id: str | None = None) -> str | None:
        answers = self.__dict__.setdefault("_handoff_answers", {})
        request_id = f"handoff_{len(answers) + 1}"
        answers[request_id] = None
        print(f"\n\033[93m[fox needs you]\033[0m {message}")
        print("Do it in the browser window and click Done there, or type s and Enter here to skip.")

        def read() -> None:
            try:
                line = input()
            except EOFError:
                return
            if answers.get(request_id) is None and line.strip().lower() == "s":
                answers[request_id] = False

        threading.Thread(target=read, daemon=True).start()
        return request_id

    def handoff_answer(self, request_id: str) -> bool | str | None:
        return self.__dict__.get("_handoff_answers", {}).get(request_id)

    def finish_handoff(self, request_id: str) -> None:
        self.__dict__.get("_handoff_answers", {}).pop(request_id, None)


class ApprovalGate(Approver):
    """Server-side approver. approve() blocks the agent's worker thread and
    parks it on a Condition until the client answers that request id, so the
    event loop stays free to serve other sessions meanwhile.

    Deliberately has no timeout. A pending approval that auto-denied after
    thirty seconds would fail tasks whenever someone stepped away, and
    auto-*approving* on a timer is the one behaviour a permission prompt must
    never have. It waits until answered or until close() ends the session —
    the same contract InjectionQueue.wait_for_message() makes.
    """

    def __init__(self, emit=None):
        self._condition = threading.Condition()
        self._answers: dict[str, bool] = {}
        self._always: set[str] = set()
        self._handoffs: set[str] = set()
        self._every_time: set[str] = set()
        self._closed = False
        self._ids = itertools.count(1)
        self._emit = emit  # callable(event_type, payload) — the session's sink

    def approve(self, tool_name: str, args: dict, reason: str, every_time: bool = False) -> bool:
        with self._condition:
            if tool_name in self._always and not every_time:
                return True
            if self._closed:
                return False
            request_id = f"approval_{next(self._ids)}"
            if every_time:
                self._every_time.add(request_id)

        if self._emit is not None:
            self._emit("approval_requested", {
                "request_id": request_id,
                "tool_name": tool_name,
                "args": args,
                "reason": reason,
                "description": describe(tool_name, args),
            })

        with self._condition:
            while request_id not in self._answers and not self._closed:
                self._condition.wait()
            self._every_time.discard(request_id)
            # A closed session denies whatever was still in flight.
            return self._answers.pop(request_id, False)

    def start_handoff(self, *, kind: str, message: str, url: str = "", task_id: str | None = None) -> str | None:
        with self._condition:
            if self._closed:
                return None
            request_id = f"handoff_{next(self._ids)}"
            self._handoffs.add(request_id)
        if self._emit is not None:
            self._emit("approval_requested", {
                "request_id": request_id,
                "kind": "handoff",
                "tool_name": "browser_ask_human",
                "args": {"url": url, "need": kind},
                "reason": "",
                "description": message,
                "task_id": task_id,
            })
        return request_id

    def handoff_answer(self, request_id: str) -> bool | str | None:
        with self._condition:
            if request_id in self._answers:
                return self._answers.pop(request_id)
            return HANDOFF_ENDED if self._closed else None

    def finish_handoff(self, request_id: str) -> None:
        with self._condition:
            self._handoffs.discard(request_id)
            self._answers.pop(request_id, None)

    def respond(self, request_id: str, approved: bool, always: bool = False,
                tool_name: str | None = None) -> None:
        with self._condition:
            if always and approved and tool_name and request_id not in self._handoffs \
                    and request_id not in self._every_time:
                self._always.add(tool_name)
            self._answers[request_id] = approved
            self._condition.notify_all()

    def close(self) -> None:
        """Ends the session: wakes every waiter with a denial. Without this a
        blocked approval would keep its thread alive after disconnect."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def describe(tool_name: str, args: dict) -> str:
    """One line naming what is about to happen. Kept here rather than in the
    TUI so the CLI and the terminal client word it identically."""
    if tool_name == "run_bash_command":
        return f"run: {args.get('command', '')}"
    if tool_name == "write_file":
        content = args.get("content", "")
        return f"write {args.get('path')} ({len(content)} chars)"
    if tool_name == "edit_file":
        return f"edit {args.get('path')}: {args.get('old_str', '')[:60]!r} → {args.get('new_str', '')[:60]!r}"
    if tool_name in ("read_file", "list_directory", "search_files"):
        return f"{tool_name} {args.get('path', '.')}"
    return f"{tool_name}({args})"
