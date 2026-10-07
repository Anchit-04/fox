import json
from dataclasses import dataclass

import memory
import permissions
from agent import IDENTITY, _emit
from config import preferences, routing
from context import COMPACT_EVERY, MAX_ITERATIONS, build_compact_input
from injection import InjectionQueue
from orchestration.dispatch import run_batch
from orchestration.policy import lane_from_proposal
from orchestration.tasks import Task, TaskRegistry
from providers import ToolResult, Turn, get_provider
from tools.todo_tool import TODO_TOOL_HANDLERS, TODO_TOOLS, TodoManager

SYSTEM_PROMPT = """You are the planning orchestrator. You never touch files or run commands yourself: you plan, and executor agents do the work.

How work runs:
- Each delegate_task call creates one task. All delegate_task calls in a single reply form one batch, and the batch runs together.
- "read" tasks only investigate. They run in parallel and cannot modify anything.
- "write" tasks may modify files. They run one at a time.
- When the batch finishes, each delegate_task call returns that task's id, status and outcome.

How to plan:
- Only put tasks in the same batch if none of them needs another's result. If a task depends on what another task finds or changes, delegate it in a later reply, after you have read that result, and put what you learned into its description.
- Write every description so it stands alone. The executor cannot see this conversation.
- Declare scope honestly: the files a task will change, or [] for a read task. Scope does not lock anything; it is compared with what the task actually wrote.
- If a task failed, read its error and decide: retry it with a better description, split it, or explain the problem to the user.
- Executors may propose work outside their task; it appears as "proposes: ..." on their result. Decide each proposal: delegate it, fold it into another task, or leave it. If you leave one, say so in your final answer.
- Each task's full result is in shared memory under its id. Use memory_read with the id, for example "t3", when the one-line summary is not enough.
- Use todo_write to keep your plan.
- When the whole job is done, reply in plain text with no tool calls. That reply is your final answer to the user.
"""


def delegate_tool(session_mode: str) -> dict:
    lanes = ["read"] if session_mode == permissions.READ_ONLY else ["read", "write"]
    return {
        "type": "function",
        "name": "delegate_task",
        "description": "Hand one task to an executor agent. Several calls in one reply run as one batch.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Complete, standalone instructions. The executor sees nothing else.",
                },
                "lane": {
                    "type": "string",
                    "enum": lanes,
                    "description": "read: investigate only, runs in parallel. write: may modify files, runs one at a time.",
                },
                "scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files this task will change. [] for a read task.",
                },
                "model_key": {
                    "type": "string",
                    "description": "Optional. A specific executor model, if one is listed under executor specialties.",
                },
            },
            "required": ["description", "lane", "scope"],
        },
    }


def _create_task(registry: TaskRegistry, args: dict) -> Task:
    description = args.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("'description' must be a non-empty string")
    scope = args.get("scope") or []
    if isinstance(scope, str):
        scope = [scope]
    if not isinstance(scope, list) or not all(isinstance(p, str) for p in scope):
        raise ValueError("'scope' must be a list of file paths")
    model_key = args.get("model_key") or None
    if model_key is not None and not isinstance(model_key, str):
        raise ValueError("'model_key' must be a string")
    return registry.create(
        description.strip(),
        lane_from_proposal(args.get("lane")),
        [p.strip() for p in scope if p.strip()],
        model_key,
    )


def _summarize(task: Task, proposals: tuple[str, ...] = ()) -> str:
    head = f"{task.id} [{task.lane.value}] {task.status.value}"
    if task.report is None:
        parts = [f"{head}: {task.error}"]
    else:
        parts = [f"{head}: {task.report.outcome}"]
        if task.report.changes:
            parts.append(f"changed {', '.join(task.report.changes)}")
        if task.report.blockers:
            parts.append(f"blockers: {'; '.join(task.report.blockers)}")
    if proposals:
        parts.append(f"proposes: {'; '.join(proposals)}")
    return " | ".join(parts)


def _ledger(registry: TaskRegistry) -> str:
    tasks = registry.all()
    if not tasks:
        return "(no tasks yet)"
    return "\n".join(f"- {_summarize(t)}" for t in tasks)


@dataclass
class _Run:
    model_key: str
    mode: str
    mem: memory.Memory
    registry: TaskRegistry
    todo: TodoManager
    event_sink: object = None
    injection_queue: InjectionQueue | None = None
    approver: object = None


def _system_prompt(run: _Run) -> str:
    parts = [IDENTITY.format(model_key=run.model_key), SYSTEM_PROMPT]
    if run.mode == permissions.READ_ONLY:
        parts.append(
            "\nThis session is read-only: delegate read tasks only. If the job needs changes, "
            "say what you would change and ask the user to switch the session to a writable mode.\n"
        )
    parts.append("\n## Executor specialties\n" + preferences.render_for_prompt())
    parts.append("\n\n## Shared memory index\n" + run.mem.read_index())
    return "".join(parts)


def _run_internal(name: str, args: dict, run: _Run) -> str:
    if name in TODO_TOOL_HANDLERS:
        result = TODO_TOOL_HANDLERS[name](run.todo, args)
        _emit(run.event_sink, "todo_updated", {"task_id": None, "checklist": run.todo.render()})
        return result
    if name in memory.MEMORY_TOOL_HANDLERS:
        return memory.MEMORY_TOOL_HANDLERS[name](run.mem, run.model_key, args) or json.dumps({"ok": True})
    return json.dumps({
        "error": f"Unknown tool: {name}. You can only use delegate_task, todo_write, memory_read and memory_write."
    })


def _run_turn(calls: list, run: _Run) -> list[ToolResult]:
    results: dict[str, str] = {}
    batch: dict[str, Task] = {}
    for tc in calls:
        _emit(run.event_sink, "tool_call", {"task_id": None, "name": tc.name, "args": tc.args})
        if tc.raw and tc.raw.get("args_parse_error"):
            results[tc.id] = json.dumps({"error": f"Could not parse the arguments for {tc.name}. Retry with valid JSON."})
        elif tc.name == "delegate_task":
            try:
                batch[tc.id] = _create_task(run.registry, tc.args)
            except ValueError as e:
                results[tc.id] = json.dumps({"error": f"Task not created: {e}"})
        else:
            results[tc.id] = _run_internal(tc.name, tc.args, run)

    if batch:
        proposals = run_batch(list(batch.values()), run.mode, mem=run.mem, event_sink=run.event_sink,
                              injection_queue=run.injection_queue, approver=run.approver)
        by_task: dict[str, list[str]] = {}
        for p in proposals:
            by_task.setdefault(p.task_id, []).append(p.text)
        for call_id, task in batch.items():
            results[call_id] = _summarize(task, tuple(by_task.get(task.id, ())))

    for tc in calls:
        _emit(run.event_sink, "tool_result", {"task_id": None, "name": tc.name, "content": results[tc.id]})
    return [ToolResult(call_id=tc.id, name=tc.name, content=results[tc.id]) for tc in calls]


def run_orchestrator(task: str, *, model_key: str | None = None, mem: memory.Memory | None = None,
                     event_sink=None, injection_queue: InjectionQueue | None = None, approver=None,
                     mode: str = permissions.WORKSPACE_WRITE) -> str:
    if mode not in permissions.MODES:
        raise ValueError(f"unknown session mode {mode!r}; expected one of {permissions.MODES}")
    model_key = model_key or routing.pick_orchestrator()
    provider = get_provider(model_key)
    run = _Run(model_key=model_key, mode=mode, mem=mem or memory.default_memory(),
               registry=TaskRegistry(), todo=TodoManager(), event_sink=event_sink,
               injection_queue=injection_queue, approver=approver)
    tools = [delegate_tool(mode)] + TODO_TOOLS + memory.MEMORY_TOOLS

    history = [Turn(role="user", text=task)]
    final_text = ""
    turns = 0
    while True:
        if injection_queue is not None:
            for msg in injection_queue.drain(None):
                history.append(Turn(role="user", text=f"[Message from human]: {msg}"))
                _emit(event_sink, "human_message_injected", {"task_id": None, "content": msg})

        response = provider.generate(history, tools, _system_prompt(run))

        if response.text:
            final_text = response.text
            _emit(event_sink, "agent_turn", {"task_id": None, "model_key": model_key, "text": response.text})
        if not response.tool_calls:
            break

        turns += 1
        if turns >= MAX_ITERATIONS:
            _emit(event_sink, "error", {"task_id": None, "message": f"hit the {MAX_ITERATIONS}-turn safety cap"})
            break

        history.append(Turn(role="assistant", text=response.text, tool_calls=response.tool_calls))
        results = _run_turn(response.tool_calls, run)

        if turns % COMPACT_EVERY == 0:
            summary = build_compact_input(task, run.todo.render(),
                                          [{"name": r.name, "content": r.content} for r in results])
            history = [Turn(role="user", text=f"{summary}\n\nEvery task so far:\n{_ledger(run.registry)}")]
        else:
            history.append(Turn(role="user", tool_results=results))

    return final_text
