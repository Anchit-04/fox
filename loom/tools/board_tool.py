import json

DIAGRAM_KINDS = ("graph", "flowchart", "sequenceDiagram")
MAX_DIAGRAM = 4000
MAX_NOTE = 6000

BOARD_NOTE = """
## The board
- The board is a pane beside the chat where you can draw while you explain. Use it when a picture helps: how parts
  of a system connect, the order of calls in a request, a plan with branches. Plain answers don't need it.
- Each `board` call adds one card: an optional title, an optional Mermaid diagram, and an optional Markdown note.
  Build an explanation up step by step with several calls, then explain it in your reply as well.
- The terminal draws Mermaid flowcharts (`graph TD`, `graph LR`) and `sequenceDiagram`. Keep node labels short and
  use under about 15 nodes per diagram. Other diagram kinds won't draw; describe them in the note instead.
- `clear: true` wipes the board before adding the card, for a new topic.
"""

BOARD_TOOL = {
    "type": "function",
    "name": "board",
    "description": (
        "Add a card to the board, a pane beside the chat that the user sees while you explain. A card can hold a "
        "title, a Mermaid diagram (flowchart or sequenceDiagram), and a Markdown note. Each call adds one card."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "A short heading for this card."},
            "diagram": {
                "type": "string",
                "description": "Mermaid source starting with `graph TD`, `graph LR`, `flowchart ...` or "
                               "`sequenceDiagram`.",
            },
            "note": {"type": "string", "description": "Markdown text shown under the diagram."},
            "clear": {"type": "boolean", "description": "Wipe the board before adding this card."},
        },
    },
}


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _kind(diagram: str) -> str:
    for line in diagram.splitlines():
        line = line.strip()
        if line and not line.startswith("%%") and line != "---":
            return line.split()[0].rstrip(";")
    return ""


def board(args: dict) -> str:
    title = str(args.get("title") or "").strip()
    diagram = str(args.get("diagram") or "").strip()
    note = str(args.get("note") or "").strip()
    if not (title or diagram or note or args.get("clear")):
        return _error("A board card needs a title, a diagram or a note.")
    if diagram:
        if diagram.startswith("```"):
            return _error("Pass the Mermaid source itself in diagram, without the ``` fence.")
        if _kind(diagram) not in DIAGRAM_KINDS:
            return _error(f"The board draws only Mermaid flowcharts (graph TD / graph LR) and sequenceDiagram, not "
                          f"{_kind(diagram) or 'an empty diagram'}. Redraw it as one of those, or describe it in "
                          "the note.")
        if len(diagram) > MAX_DIAGRAM:
            return _error(f"The diagram is over {MAX_DIAGRAM} characters. Split it into smaller cards.")
    if len(note) > MAX_NOTE:
        return _error(f"The note is over {MAX_NOTE} characters. Split it into smaller cards.")
    what = f'"{title}"' if title else "the card"
    if not (title or diagram or note):
        return "Cleared the board."
    return (f"Added {what} to the board. The user sees it in the pane beside the chat (ctrl+b). If a diagram has a "
            "mistake, the pane shows its source instead of a drawing.")


BOARD_TOOLS = [BOARD_TOOL]
BOARD_TOOL_HANDLERS = {"board": board}
