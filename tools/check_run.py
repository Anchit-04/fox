import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TERMINAL = {"done", "failed", "cancelled", "escalated"}


def analyze(state: dict) -> dict:
    lanes, last, running = {}, {}, set()
    max_readers, writer_overlaps = 0, []
    audits, proposals, claimed = [], [], set()
    for turn in state["turns"]:
        kind, p = turn["type"], turn["payload"]
        tid = p.get("task_id")
        if kind == "task_created":
            lanes[tid] = p.get("lane")
        elif kind == "task_status_changed" and tid is not None:
            last[tid] = p["status"]
            if p["status"] == "running":
                if lanes.get(tid) == "write":
                    others = [t for t in running if lanes.get(t) == "write"]
                    if others:
                        writer_overlaps.append((tid, others))
                running.add(tid)
                max_readers = max(max_readers, sum(1 for t in running if lanes.get(t) == "read"))
            elif p["status"] in TERMINAL:
                running.discard(tid)
        elif kind == "scope_audit":
            audits.append(p)
        elif kind == "proposal_made":
            proposals.append((tid, p["text"]))
        elif kind == "tool_result" and tid is None and p.get("name") == "delegate_task":
            for match in re.finditer(r"\| changed ([^|]+)", p.get("content") or ""):
                claimed.update(f.strip() for f in match.group(1).split(","))
    return {
        "lanes": lanes, "last": last, "max_readers": max_readers, "writer_overlaps": writer_overlaps,
        "audits": audits, "proposals": proposals, "claimed": claimed,
        "readers": sum(1 for lane in lanes.values() if lane == "read"),
    }


def git_changed(fixture: Path) -> set[str]:
    out = subprocess.run(["git", "-C", str(fixture), "status", "--porcelain"],
                         capture_output=True, text=True, check=True).stdout
    return {line[3:].strip().replace("\\", "/") for line in out.splitlines() if line.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a Fox orchestrator session against Phase 1's exit criteria.")
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
        ("readers ran concurrently",
         None if r["readers"] < 2 else r["max_readers"] >= 2,
         f"peak {r['max_readers']} readers at once, {r['readers']} read tasks in total"),
        ("writers never overlapped", not r["writer_overlaps"],
         f"overlaps: {r['writer_overlaps']}" if r["writer_overlaps"] else "one writer at a time"),
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
    if args.fixture:
        on_disk = git_changed(args.fixture)
        seen = {w for a in r["audits"] for w in a["written"]}
        print(f"  changed on disk (git):     {sorted(on_disk)}")
        print(f"  claimed in reports:        {sorted(r['claimed'])}")
        print(f"  seen by the audit:         {sorted(seen)}")
        print(f"  on disk but never claimed: {sorted(on_disk - r['claimed']) or 'none'}")
        print(f"  claimed but not on disk:   {sorted(r['claimed'] - on_disk) or 'none'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
