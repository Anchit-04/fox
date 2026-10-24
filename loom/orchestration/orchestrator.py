import json
from dataclasses import dataclass, field

import memory
import permissions
from agent import ALL_TOOLS, IDENTITY, _emit, run_tool_call
from approval import DenyApprover
from config import preferences, routing
import context
from context import MAX_ITERATIONS, build_compact_input
from injection import InjectionQueue
from orchestration.claims import FileClaims
from orchestration.dispatch import Finished, TaskInbox, start_task
from orchestration.policy import READ_LANE_TOOLS, lane_from_proposal
from orchestration.tasks import Lane, Task, TaskRegistry
from providers import ToolResult, Turn, get_provider
from tools import file_tools
from tools.todo_tool import TodoManager

SYSTEM_PROMPT = """You are the orchestrator. You can read and edit files and run commands yourself, and you can hand tasks to executor agents.

What to do yourself and what to delegate:
- Make connected edits yourself: changes that depend on each other or on decisions you have made. You hold the whole picture; an executor only sees its own task.
- Delegate investigation: reading many files, finding every place something is used, working out how part of the codebase behaves. Read tasks run in parallel, and their reading stays out of your context.
- Delegate a write when it is independent of your own edits, for example docs or tests in files you are not changing.
- Do not delegate anything you could finish in one or two tool calls.

How work runs:
- delegate_task starts one task in the background and returns at once with its id. You keep working while it runs.
- When a task finishes, its result arrives as a "[task finished]" message before your next turn.
- Hand off independent pieces first, then do your own part, so the executors work while you do.
- Call wait_for_tasks when you have nothing else to do until a result arrives.
- "read" tasks only investigate and cannot modify anything.
- "write" tasks may modify files. A write task claims the files in its scope while it runs: nobody else, including you, can write them until it finishes. Write tasks on different files run at the same time.
- You cannot finish while tasks are still running.

How to plan:
- Only start a task once everything it depends on is known. If it needs what another task finds or changes, wait for that result and put what you learned into its description.
- Write every description so it stands alone. The executor cannot see this conversation.
- Declare scope honestly: every file a write task will change, or [] for a read task. A write task can only start if none of its files is claimed by another running task.
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
        "description": "Start one task on an executor agent in the background. Returns at once; the result arrives later.",
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
                    "description": "read: investigate only. write: may modify the files in its scope.",
                },
                "scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files this task will change; it claims them while it runs. [] for a read task.",
                },
                "model_key": {
                    "type": "string",
                    "description": "Optional. A specific executor model, if one is listed under executor specialties.",
                },
            },
            "required": ["description", "lane", "scope"],
        },
    }


WAIT_TOOL = {
    "type": "function",
    "name": "wait_for_tasks",
    "description": "Block until at least one running task finishes, then return its result.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

WAIT_SECONDS = 600


def orchestrator_tools(session_mode: str) -> list[dict]:
    own = [t for t in ALL_TOOLS if session_mode != permissions.READ_ONLY or t["name"] in READ_LANE_TOOLS]
    return [delegate_tool(session_mode), WAIT_TOOL] + own


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
    lane = lane_from_proposal(args.get("lane"))
    scope = [p.strip() for p in scope if p.strip()]
    if lane is Lane.WRITE and not scope:
        raise ValueError("a write task must list every file it will change in 'scope'")
    return registry.create(description.strip(), lane, scope, model_key)


def _summarize(task: Task, proposals: tuple[str, ...] = ()) -> str:
    head = f"{task.id} [{task.lane.value}] {task.status.value}"
    if not task.is_terminal:
        return f"{head}: still running"
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
    claims: FileClaims = field(default_factory=FileClaims)
    inbox: TaskInbox = field(default_factory=TaskInbox)
    running: dict[str, Task] = field(default_factory=dict)


def _system_prompt(run: _Run) -> str:
    parts = [IDENTITY.format(model_key=run.model_key), SYSTEM_PROMPT]
    if run.mode == permissions.READ_ONLY:
        parts.append(
            "\nThis session is read-only: you and every task can only read. If the job needs changes, "
            "say what you would change and ask the user to switch the session to a writable mode.\n"
        )
    parts.append("\n## Executor specialties\n" + preferences.render_for_prompt())
    parts.append("\n\n## Shared memory index\n" + run.mem.read_index())
    return "".join(parts)


def _run_own(tc, run: _Run) -> str:
    result = run_tool_call(tc, mode=run.mode, approver=run.approver, model_key=run.model_key,
                           todo_manager=run.todo, mem=run.mem, event_sink=run.event_sink, claims=run.claims)
    if tc.name == "todo_write":
        _emit(run.event_sink, "todo_updated", {"task_id": None, "checklist": run.todo.render()})
    return result


def _delegate(tc, run: _Run) -> str:
    try:
        task = _create_task(run.registry, tc.args)
    except ValueError as e:
        return json.dumps({"error": f"Task not created: {e}"})
    run.running[task.id] = task
    taken = start_task(task, run.mode, claims=run.claims, inbox=run.inbox, mem=run.mem,
                       event_sink=run.event_sink, injection_queue=run.injection_queue, approver=run.approver)
    if taken:
        del run.running[task.id]
        return json.dumps({"error": f"{task.id} not started: {', '.join(taken)} already claimed by a running "
                                    "task. Wait for it to finish, or re-scope this task."})
    return f"{task.id} started [{task.lane.value}]. Its result will arrive as a message when it finishes."


def _finished_text(run: _Run, finished: list[Finished]) -> str:
    for f in finished:
        run.running.pop(f.task.id, None)
    return "\n".join(f"[task finished] {_summarize(f.task, f.proposals)}" for f in finished)


def _wait(run: _Run) -> str:
    if not run.running:
        return "No tasks are running."
    finished = run.inbox.wait(WAIT_SECONDS)
    if not finished:
        return f"Nothing finished in {WAIT_SECONDS}s. Still running: {', '.join(run.running)}"
    return _finished_text(run, finished)


def _run_turn(calls: list, run: _Run) -> list[ToolResult]:
    results: dict[str, str] = {}
    for tc in calls:
        _emit(run.event_sink, "tool_call", {"task_id": None, "name": tc.name, "args": tc.args})
        if tc.raw and tc.raw.get("args_parse_error"):
            results[tc.id] = json.dumps({"error": f"Could not parse the arguments for {tc.name}. Retry with valid JSON."})
        elif tc.name == "delegate_task":
            results[tc.id] = _delegate(tc, run)
        elif tc.name == "wait_for_tasks":
            results[tc.id] = _wait(run)
        else:
            results[tc.id] = _run_own(tc, run)
        _emit(run.event_sink, "tool_result", {"task_id": None, "name": tc.name, "content": results[tc.id]})
    return [ToolResult(call_id=tc.id, name=tc.name, content=results[tc.id]) for tc in calls]


def _wait_for_everything(run: _Run) -> None:
    while run.running:
        _finished_text(run, run.inbox.wait())


def run_orchestrator(task: str, *, model_key: str | None = None, mem: memory.Memory | None = None,
                     event_sink=None, injection_queue: InjectionQueue | None = None, approver=None,
                     mode: str = permissions.WORKSPACE_WRITE) -> str:
    if mode not in permissions.MODES:
        raise ValueError(f"unknown session mode {mode!r}; expected one of {permissions.MODES}")
    model_key = model_key or routing.pick_orchestrator()
    provider = get_provider(model_key)
    run = _Run(model_key=model_key, mode=mode, mem=mem or memory.default_memory(),
               registry=TaskRegistry(), todo=TodoManager(), event_sink=event_sink,
               injection_queue=injection_queue, approver=approver or DenyApprover())
    tools = orchestrator_tools(mode)
    file_tools.clear_approved_paths()

    history = [Turn(role="user", text=task)]
    final_text = ""
    turns = 0
    try:
        while True:
            if injection_queue is not None:
                for msg in injection_queue.drain(None):
                    history.append(Turn(role="user", text=f"[Message from human]: {msg}"))
                    _emit(event_sink, "human_message_injected", {"task_id": None, "content": msg})
            _deliver(history, run, _finished_text(run, run.inbox.drain()))

            response = provider.generate(history, tools, _system_prompt(run))

            if response.text:
                final_text = response.text
                _emit(event_sink, "agent_turn", {"task_id": None, "model_key": model_key, "text": response.text})
            if not response.tool_calls:
                if not run.running:
                    break
                history.append(Turn(role="assistant", text=response.text or "(waiting for running tasks)",
                                    raw=response.raw))
                _deliver(history, run, _wait(run) + "\n\nTasks were still running, so you cannot finish yet. "
                                                    "Review the results above before your final answer.")
                continue

            turns += 1
            if turns >= MAX_ITERATIONS:
                _emit(event_sink, "error", {"task_id": None, "message": f"hit the {MAX_ITERATIONS}-turn safety cap"})
                break

            history.append(Turn(role="assistant", text=response.text, tool_calls=response.tool_calls, raw=response.raw))
            results = _run_turn(response.tool_calls, run)

            history.append(Turn(role="user", tool_results=results))
            if context.should_compact(history):
                summary = build_compact_input(task, run.todo.render(),
                                              [{"name": r.name, "content": r.content} for r in results])
                history = [Turn(role="user", text=f"{summary}\n\nEvery task so far:\n{_ledger(run.registry)}")]
    finally:
        _wait_for_everything(run)
        file_tools.clear_approved_paths()
    return final_text


def _deliver(history: list, run: _Run, text: str) -> None:
    if not text:
        return
    history.append(Turn(role="user", text=text))
    _emit(run.event_sink, "task_results_delivered", {"task_id": None, "content": text})
