import json
from dataclasses import dataclass, field

import git_context
import memory
import paths
import permissions
from agent import ALL_TOOLS, IDENTITY, restore_todos, run_tool_call
from events import emit
from approval import DenyApprover
from calls import generate_or_pause
from config import roster, routing
import context
from context import MAX_ITERATIONS, build_compact_input
from history import History
from injection import InjectionQueue
from orchestration.claims import FileClaims
from orchestration.dispatch import Finished, TaskInbox, continue_task, start_task
from orchestration.policy import READ_LANE_TOOLS, lane_from_proposal
from orchestration.tasks import Lane, Task, TaskRegistry, UnknownTask
from providers import ToolOutput, ToolResult, Turn, get_provider, has_vision
from sandbox.sandbox_client import SHELL_NOTE
from tools import board_tool, browser_tools, file_tools, web
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
- Notes that agents leave in shared memory arrive the same way, as a "[memory]" message.
- Hand off independent pieces first, then do your own part, so the executors work while you do.
- Call wait_for_tasks when you have nothing else to do until a result arrives.
- "read" tasks only investigate and cannot modify anything.
- You and every executor can open web pages in Fox's browser. Use it yourself when the user wants to see or use a page; delegate browsing that is research, as a read task.
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

""" + SHELL_NOTE


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
                    "description": ("Optional. A name from 'Workers you can delegate to'. Leave it out to "
                                    "use the default worker."),
                },
            },
            "required": ["description", "lane", "scope"],
        },
    }


CONTINUE_TOOL = {
    "type": "function",
    "name": "continue_task",
    "description": ("Send a finished task's executor a follow-up, for example a missed case. It carries on with "
                    "everything it already read, under the same task id, and its new result arrives as a message."),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The finished task, for example t3."},
            "message": {"type": "string", "description": "What it should do next."},
        },
        "required": ["task_id", "message"],
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
    return [delegate_tool(session_mode), CONTINUE_TOOL, WAIT_TOOL] + own


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
    notes_seen: int = 0
    browser: "browser_tools.BrowserSession | None" = None
    journal: object = None


def _system_prompt(run: _Run) -> str:
    parts = [IDENTITY.format(model_key=run.model_key), SYSTEM_PROMPT]
    if run.mode == permissions.READ_ONLY:
        parts.append(
            "\nThis session is read-only: you and every task can only read. If the job needs changes, "
            "say what you would change and ask the user to switch the session to a writable mode.\n"
        )
    parts.append(web.WEB_NOTE)
    parts.append(board_tool.BOARD_NOTE)
    parts.append(git_context.prompt_note(paths.workspace()))
    parts.append(browser_tools.prompt_note(run.model_key))
    parts.append(roster.render_for_prompt(roster.load(), routing.default_worker(run.model_key)))
    return "".join(parts)


def _new_notes(run: _Run) -> str:
    notes = run.mem.notes(skip=run.notes_seen)
    run.notes_seen += len(notes)
    if not notes:
        return ""
    return "[memory] New notes in shared memory (memory_read shows one in full):\n" + "\n".join(notes)


def _run_own(tc, run: _Run) -> "str | ToolOutput":
    handlers = run.browser.handlers() if run.browser is not None else None
    result = run_tool_call(tc, mode=run.mode, approver=run.approver, model_key=run.model_key,
                           todo_manager=run.todo, mem=run.mem, event_sink=run.event_sink, claims=run.claims,
                           extra_handlers=handlers)
    if tc.name == "todo_write":
        emit(run.event_sink, "todo_updated", {"task_id": None, "checklist": run.todo.render()})
    return result


def _delegate(tc, run: _Run) -> str:
    try:
        task = _create_task(run.registry, tc.args)
    except ValueError as e:
        return json.dumps({"error": f"Task not created: {e}"})
    run.running[task.id] = task
    taken = start_task(task, run.mode, claims=run.claims, inbox=run.inbox, mem=run.mem,
                       event_sink=run.event_sink, injection_queue=run.injection_queue, approver=run.approver,
                       journal=run.journal, default_model=run.model_key)
    if taken:
        del run.running[task.id]
        return json.dumps({"error": f"{task.id} not started: {', '.join(taken)} already claimed by a running "
                                    "task. Wait for it to finish, or re-scope this task."})
    return f"{task.id} started [{task.lane.value}]. Its result will arrive as a message when it finishes."


def _finished_text(run: _Run, finished: list[Finished]) -> str:
    for f in finished:
        run.running.pop(f.task.id, None)
    return "\n".join(f"[task finished] {_summarize(f.task, f.proposals)}{_run_label(f.task)}" for f in finished)


def _run_label(task: Task) -> str:
    return f" (run {task.run})" if task.run > 1 else ""


def reopen(run: _Run, task_id: str, message: str) -> str | None:
    if run.journal is None:
        return "this chat has no stored histories, so its tasks cannot be continued"
    try:
        task = run.registry.get(task_id)
    except UnknownTask:
        return f"there is no task {task_id}"
    if not task.is_terminal:
        return f"{task_id} is still running; your message was queued for it instead"
    taken = continue_task(task, message, run.mode, claims=run.claims, inbox=run.inbox, mem=run.mem,
                          journal=run.journal, event_sink=run.event_sink, injection_queue=run.injection_queue,
                          approver=run.approver, default_model=run.model_key)
    if taken:
        return f"{task_id} cannot continue: {', '.join(taken)} is claimed by a running task"
    run.running[task.id] = task
    return None


def _continue(tc, run: _Run) -> str:
    task_id = str(tc.args.get("task_id") or "")
    message = str(tc.args.get("message") or "").strip()
    if not message:
        return json.dumps({"error": "message must say what the executor should do next"})
    refused = reopen(run, task_id, f"[Message from the orchestrator]: {message}")
    if refused:
        return json.dumps({"error": refused})
    return f"{task_id} continues (run {run.registry.get(task_id).run}). Its result will arrive as a message."


def _wait(run: _Run) -> str:
    if not run.running:
        return "No tasks are running."
    finished = run.inbox.wait(WAIT_SECONDS)
    if not finished:
        return f"Nothing finished in {WAIT_SECONDS}s. Still running: {', '.join(run.running)}"
    return _finished_text(run, finished)


def _run_turn(calls: list, run: _Run, history: History) -> list[ToolResult]:
    results: dict[str, str] = {}
    images: dict[str, list] = {}
    for tc in calls:
        emit(run.event_sink, "tool_call", {"task_id": None, "name": tc.name, "args": tc.args})
        if tc.raw and tc.raw.get("args_parse_error"):
            results[tc.id] = json.dumps({"error": f"Could not parse the arguments for {tc.name}. Retry with valid JSON."})
        elif tc.name == "delegate_task":
            results[tc.id] = _delegate(tc, run)
        elif tc.name == "wait_for_tasks":
            results[tc.id] = _wait(run)
        elif tc.name == "continue_task":
            results[tc.id] = _continue(tc, run)
        else:
            result = _run_own(tc, run)
            if isinstance(result, ToolOutput):
                images[tc.id] = result.images if has_vision(run.model_key) else []
                result = result.text + (f"\n[{len(images[tc.id])} screenshot attached]" if images[tc.id] else "")
            results[tc.id] = result
        emit(run.event_sink, "tool_result", {"task_id": None, "name": tc.name, "content": results[tc.id]})
        history.add_result(ToolResult(call_id=tc.id, name=tc.name, content=results[tc.id],
                                      images=images.get(tc.id, [])))
    return [ToolResult(call_id=tc.id, name=tc.name, content=results[tc.id]) for tc in calls]


def _wait_for_everything(run: _Run) -> None:
    while run.running:
        _finished_text(run, run.inbox.wait())


def run_orchestrator(task: str, *, model_key: str | None = None, mem: memory.Memory | None = None,
                     event_sink=None, injection_queue: InjectionQueue | None = None, approver=None,
                     mode: str = permissions.WORKSPACE_WRITE, notes_seen: int = 0, journal=None,
                     history: History | None = None, registry: TaskRegistry | None = None,
                     wait_first: bool = False, on_reopen=None) -> str:
    if mode not in permissions.MODES:
        raise ValueError(f"unknown session mode {mode!r}; expected one of {permissions.MODES}")
    model_key = model_key or routing.pick_chat()
    provider = get_provider(model_key)
    run = _Run(model_key=model_key, mode=mode, mem=mem or memory.default_memory(),
               registry=registry or TaskRegistry(), todo=TodoManager(), event_sink=event_sink,
               injection_queue=injection_queue, approver=approver or DenyApprover(), notes_seen=notes_seen,
               journal=journal)
    run.browser = browser_tools.BrowserSession(model_key, mode=mode, approver=run.approver, event_sink=event_sink)
    tools = orchestrator_tools(mode)
    file_tools.clear_approved_paths()

    history = history or History(task, journal.recorder(None) if journal else None, notes_seen=notes_seen)
    restore_todos(run.todo, history.turns)
    run.browser.offer_all = browser_tools.used_browser(history.turns)
    if on_reopen is not None:
        on_reopen(lambda task_id, message: reopen(run, task_id, f"[Message from human]: {message}"))
    system_prompt = _system_prompt(run)
    final_text = ""
    turns = 0
    try:
        if wait_first and injection_queue is not None:
            emit(event_sink, "awaiting_input", {"task_id": None})
            messages = injection_queue.wait_for_message(None)
            if messages is None:
                return final_text
            history.add_human_messages(messages, None, event_sink)
        while True:
            if injection_queue is not None:
                history.add_human_messages(injection_queue.drain(None), None, event_sink)
            arrived = (_finished_text(run, run.inbox.drain()), _new_notes(run))
            _deliver(history, run, "\n\n".join(text for text in arrived if text))

            history.keep_latest_images(browser_tools.KEEP_SCREENSHOTS)
            provider, response = generate_or_pause(provider, history, tools + run.browser.tools(), system_prompt,
                                                   model_key=model_key, task_id=None, event_sink=event_sink,
                                                   injection_queue=injection_queue)

            if response.text:
                final_text = response.text
                emit(event_sink, "agent_turn", {"task_id": None, "model_key": model_key, "text": response.text})
            if not response.tool_calls:
                if not run.running:
                    if injection_queue is None:
                        break
                    history.add(Turn(role="assistant", text=response.text or "(no reply)", raw=response.raw))
                    emit(event_sink, "awaiting_input", {"task_id": None})
                    messages = injection_queue.wait_for_message(None)
                    if messages is None:
                        break
                    history.add_human_messages(messages, None, event_sink)
                    continue
                history.add(Turn(role="assistant", text=response.text or "(waiting for running tasks)",
                                 raw=response.raw))
                _deliver(history, run, _wait(run) + "\n\nTasks were still running, so you cannot finish yet. "
                                                    "Review the results above before your final answer.")
                continue

            turns += 1
            if turns >= MAX_ITERATIONS:
                emit(event_sink, "error", {"task_id": None, "message": f"hit the {MAX_ITERATIONS}-turn safety cap"})
                break

            history.add(Turn(role="assistant", text=response.text, tool_calls=response.tool_calls, raw=response.raw))
            results = _run_turn(response.tool_calls, run, history)

            if context.should_compact(history.turns):
                summary = build_compact_input(task, run.todo.render(),
                                              [{"name": r.name, "content": r.content} for r in results])
                history.replace_with_summary(f"{summary}\n\nEvery task so far:\n{_ledger(run.registry)}")
    finally:
        _wait_for_everything(run)
        run.browser.close()
        file_tools.clear_approved_paths()
    return final_text


def _deliver(history: History, run: _Run, text: str) -> None:
    if not text:
        return
    history.add(Turn(role="user", text=text), notes_seen=run.notes_seen)
    emit(run.event_sink, "task_results_delivered", {"task_id": None, "content": text})
