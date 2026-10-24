import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TERMINAL = {"done", "failed", "cancelled", "escalated"}
WRITE_TOOLS = {"write_file", "edit_file"}
CONTROL_TOOLS = {"delegate_task", "wait_for_tasks"}


def _norm(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _succeeded(content: str | None) -> bool:
    try:
        return json.loads(content or "").get("success") is True
    except (ValueError, AttributeError):
        return False


def analyze(state: dict) -> dict:
    lanes, scopes, last, running = {}, {}, {}, set()
    max_readers, max_writers = 0, 0
    audits, proposals, claimed, own_writes = [], [], set(), set()
    overlapping_calls, conflicts = 0, []
    pending_write = None
    for turn in state["turns"]:
        kind, p = turn["type"], turn["payload"]
        tid = p.get("task_id")
        if kind == "task_created":
            lanes[tid] = p.get("lane")
            scopes[tid] = {_norm(s) for s in p.get("scope") or []}
        elif kind == "task_status_changed" and tid is not None:
            last[tid] = p["status"]
            if p["status"] == "running":
                if lanes.get(tid) == "write":
                    for other in running:
                        shared = scopes.get(tid, set()) & scopes.get(other, set())
                        if lanes.get(other) == "write" and shared:
                            conflicts.append(f"{tid} and {other} both held {sorted(shared)}")
                running.add(tid)
                max_readers = max(max_readers, sum(1 for t in running if lanes.get(t) == "read"))
                max_writers = max(max_writers, sum(1 for t in running if lanes.get(t) == "write"))
            elif p["status"] in TERMINAL:
                running.discard(tid)
        elif kind == "scope_audit":
            audits.append(p)
        elif kind == "proposal_made":
            proposals.append((tid, p["text"]))
        elif kind == "tool_call" and tid is None:
            if p.get("name") not in CONTROL_TOOLS and running:
                overlapping_calls += 1
            path = (p.get("args") or {}).get("path")
            pending_write = _norm(path) if p.get("name") in WRITE_TOOLS and path else None
        elif kind == "tool_result" and tid is None:
            if p.get("name") in WRITE_TOOLS and pending_write and _succeeded(p.get("content")):
                own_writes.add(pending_write)
                holders = [t for t in running if lanes.get(t) == "write" and pending_write in scopes.get(t, set())]
                if holders:
                    conflicts.append(f"orchestrator wrote {pending_write} while {holders} held it")
            pending_write = None
        if kind in ("task_results_delivered", "tool_result") and tid is None:
            for match in re.finditer(r"\| changed ([^|\n]+)", p.get("content") or ""):
                claimed.update(_norm(f.strip()) for f in match.group(1).split(","))
    return {
        "lanes": lanes, "last": last, "max_readers": max_readers, "max_writers": max_writers,
        "audits": audits, "proposals": proposals, "claimed": claimed, "own_writes": own_writes,
        "overlapping_calls": overlapping_calls, "conflicts": conflicts,
        "readers": sum(1 for lane in lanes.values() if lane == "read"),
    }


def git_changed(fixture: Path) -> set[str]:
    out = subprocess.run(["git", "-C", str(fixture), "status", "--porcelain"],
                         capture_output=True, text=True, check=True).stdout
    return {line[3:].strip().replace("\\", "/") for line in out.splitlines() if line.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a Fox orchestrator session against its exit criteria.")
    parser.add_argument("session_file", type=Path)
    parser.add_argument("--fixture", type=Path, default=None)
    args = parser.parse_args()
    state = json.loads(args.session_file.read_text(encoding="utf-8"))
    r = analyze(state)

    write_audits = [a for a in r["audits"] if a["lane"] == "write"]
    mispredicted = [a for a in write_audits if a["mispredicted"]]
    unnamed = {t: s for t, s in r["last"].items() if s not in TERMINAL}
    missing = [t for t in r["lanes"] if t not in r["last"]]
    no_report = [t for t in r["lanes"] if r["last"].get(t) == "failed"]

    checks = [
        ("orchestrator worked while tasks ran",
         None if not r["lanes"] else r["overlapping_calls"] > 0,
         f"{r['overlapping_calls']} of its own tool calls ran while a task was running"),
        ("readers ran concurrently",
         None if r["readers"] < 2 else r["max_readers"] >= 2,
         f"peak {r['max_readers']} readers at once, {r['readers']} read tasks in total"),
        ("no file was written by two owners at once", not r["conflicts"],
         "; ".join(r["conflicts"]) or f"peak {r['max_writers']} write tasks at once, no shared files"),
        ("every task ended in a named state", not unnamed and not missing,
         f"{len(r['last'])} tasks: " + ", ".join(f"{t}={s}" for t, s in r["last"].items())),
        ("misprediction rate recorded", bool(write_audits),
         f"{len(mispredicted)}/{len(write_audits)} write tasks wrote outside their declared scope"),
    ]
    failed = False
    print(f"session status: {state['status']}\n")
    for name, ok, detail in checks:
        mark = "n/a " if ok is None else ("PASS" if ok else "FAIL")
        failed |= ok is False
        print(f"[{mark}] {name}: {detail}")

    print("\nfor deciding what's next:")
    print(f"  failed tasks: {no_report or 'none'}")
    for a in mispredicted:
        print(f"  {a['task_id']} declared {a['declared']} but wrote {a['undeclared']}")
    print(f"  proposals: {r['proposals'] or 'none'}")
    print(f"  written by the orchestrator itself: {sorted(r['own_writes']) or 'none'}")
    if args.fixture:
        on_disk = git_changed(args.fixture)
        seen = {w for a in r["audits"] for w in a["written"]}
        accounted = r["claimed"] | r["own_writes"]
        print(f"  changed on disk (git):     {sorted(on_disk)}")
        print(f"  claimed in reports:        {sorted(r['claimed'])}")
        print(f"  seen by the audit:         {sorted(seen)}")
        print(f"  on disk but never claimed: {sorted(on_disk - accounted) or 'none'}")
        print(f"  claimed but not on disk:   {sorted(r['claimed'] - on_disk) or 'none'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
