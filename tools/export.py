import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from paths import db_path
from store import Store


def markdown(view: dict) -> str:
    lines = [f"# {view['task']}", "",
             f"status: {view['status']} · model: {view['model_key']} · {view['started_at']} → {view['ended_at']}", ""]
    for event in view["turns"]:
        who = event["task_id"] or "main"
        lines.append(f"- `{event['ts']}` **{event['type']}** ({who}) {json.dumps(event['payload'], ensure_ascii=False)}")
    if view["final_text"]:
        lines += ["", "## Final answer", "", view["final_text"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a session from fox.db as JSON or markdown.")
    parser.add_argument("session")
    parser.add_argument("--format", choices=["json", "md"], default="md")
    parser.add_argument("--db", type=Path, default=db_path())
    args = parser.parse_args()
    with Store.open(args.db, readonly=True) as store:
        view = store.session_view(args.session)
    if view is None:
        sys.exit(f"no session {args.session!r} in {args.db}")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(view, indent=2, ensure_ascii=False) if args.format == "json" else markdown(view), end="")


if __name__ == "__main__":
    main()
