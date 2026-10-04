import json

from orchestration.tasks import Report

REPORT_TOOL = {
    "type": "function",
    "name": "report",
    "description": (
        "Finish your task. Call this exactly once, as your very last action, instead of "
        "replying in plain text. Your task is not complete until you call it. Be honest: "
        "if you could not finish, say so in outcome and list what stopped you in blockers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "description": "What happened, in one or two sentences.",
            },
            "changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paths of files you changed. Empty if you changed nothing.",
            },
            "validation": {
                "type": "string",
                "description": "What you ran or checked to confirm it works. Write 'none' if you checked nothing.",
            },
            "blockers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything that stopped or limited the work. Empty if nothing did.",
            },
        },
        "required": ["outcome", "changes", "validation", "blockers"],
    },
}

EXECUTOR_TOOLS = [REPORT_TOOL]

REPORT_INSTRUCTIONS = (
    "\n\nWhen you are finished, call the `report` tool instead of replying in plain text. "
    "A task that ends without calling `report` counts as failed."
)


def _text(args: dict, field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field}' must be a non-empty string")
    return value.strip()


def _string_list(args: dict, field: str) -> list[str]:
    value = args.get(field, [])
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"'{field}' must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def parse_report(args: dict) -> Report:
    return Report(
        outcome=_text(args, "outcome"),
        changes=_string_list(args, "changes"),
        validation=_text(args, "validation"),
        blockers=_string_list(args, "blockers"),
    )


class ReportCollector:
    def __init__(self):
        self.report: Report | None = None

    @property
    def done(self) -> bool:
        return self.report is not None

    def handle(self, args: dict) -> str:
        if self.report is not None:
            return json.dumps({"error": "You already reported. Your task is over; stop here."})
        try:
            self.report = parse_report(args)
        except ValueError as e:
            return json.dumps({"error": f"Report not accepted: {e}. Call report again with that fixed."})
        return json.dumps({"ok": True})

    def handlers(self) -> dict:
        return {"report": self.handle}
