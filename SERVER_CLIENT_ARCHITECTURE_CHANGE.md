# Server–client architecture change

How Fox moved from one server process running every session to a hub plus one
worker process per session. Written step by step as we built it (branch
`server-per-session`, started 2026-11-02), meant for revising before interviews.

Each step has the same parts: **the problem**, **the concept**, **what we
changed**, **the code**, **why this way (and what we rejected)**, and
**interview questions** with short answers.

---

## The big picture

### Before

```
TUI ──WebSocket──> server.py  (ONE Python process)
                   ├ session A: thread + its executor threads
                   ├ session B: thread + its executor threads
                   └ workspace: one global folder, set at startup
```

- Every session is a **thread** in the same process. A crash, a hang or
  runaway memory in one session can take down all of them.
- The workspace is a **process-wide global**, so every session works in the
  same folder. A second project means restarting the server.

### After

```
TUI ──WebSocket──> hub (server.py)          no agent code at all
                    │  stdin/stdout pipes, one JSON object per line
          ┌─────────┼─────────┐
          ▼         ▼         ▼
       worker A  worker B  worker C          one OS process per session
       (folder1) (folder1) (folder2)         each with its own workspace
          │         │         │
          └────── fox.db (SQLite, WAL) ──────  every process writes its own events
```

- **Hub** = receptionist: starts workers, routes messages to the right one,
  relays their events back to the TUIs watching, answers machine-wide
  questions (keys, projects, session list).
- **Worker** = one office per session: sets its own workspace, runs the agent
  or orchestrator, writes its events to the database, exits when done.
- **Project** = a folder sessions run in. One project has many sessions.

### The steps

| # | Step | Status |
|---|---|---|
| 1 | Lazy provider SDK imports | ✅ |
| 2 | `paths.validate_workspace` and `workspace_key` | ✅ |
| 3 | Store: `migrate` flag, `workspace_key` migration, `projects()` / `sessions()` | ✅ |
| 4 | API keys across processes | ✅ |
| 5 | `awaiting_input` event | ✅ |
| 6 | `worker.py` | ✅ |
| 7 | The hub (`server.py`) | ✅ |
| 8 | Tests: fake worker, scripted worker, hub | ✅ |
| 9 | Exit checks | ✅ without models · real-model run pending |

Jump to: [the final architecture](#the-final-architecture) ·
[protocol reference](#protocol-reference) · [where we changed the spec](#where-we-changed-pivot_a-and-why) ·
[known limits](#known-limits)

---

## Step 1 — Lazy provider SDK imports

### The problem

Importing Fox took **4.5–6.7 seconds**. `loom/providers/config.py` imported
all three provider adapters at the top, and each adapter imports its SDK at
the top of its file:

```
providers/config.py
  from .gemini import GeminiProvider            → google.genai   (~3–5 s)
  from .openai_compatible import ...Provider    → openai         (~1.2 s)
  from .anthropic import AnthropicProvider      → (already lazy inside __init__)
```

With one server process this cost was paid once. With one process **per
session**, every new session would wait those seconds before it could do
anything, even if it only ever used one provider.

### The concept

- **Python imports are executed code.** `import x` runs the whole module the
  first time (defining classes, building objects, importing *its* imports),
  then caches it in `sys.modules`. Big SDKs do a lot of work at import time.
- **Lazy import** = delay the import until the code that needs it actually
  runs. You pay only for what you use.
- **Why Windows makes this matter:** on Linux, a server can `fork()`. The child
  process starts as a copy of the parent's memory, with every module already
  imported, so start-up is nearly free. Windows has no `fork`; a new Python
  process (`spawn`) starts from nothing and re-imports everything.
- **Measure first:** `python -X importtime -c "import providers"` prints how
  long each module took (self time and cumulative). That's how we found
  `google.genai` was most of the cost.

### What we changed

1. The registry names each provider by a **string** (its module name) instead
   of holding the class object.
2. A lookup table maps module name → class name.
3. `provider_class(module)` imports that module only when called, using
   `importlib.import_module`.
4. `get_provider` calls `provider_class`, so the SDK loads the first time a
   model from that provider is actually built.

Public names (`MODEL_REGISTRY`, `get_provider`, `get_tier`, `call_cost`) are
unchanged, so nothing that uses them had to change.

### The code

```python
# loom/providers/config.py
from importlib import import_module

from .base import Usage

PROVIDER_CLASSES = {
    "anthropic": "AnthropicProvider",
    "gemini": "GeminiProvider",
    "openai_compatible": "OpenAICompatibleProvider",
}

MODEL_REGISTRY = {
    "gemini-lite": {"provider": "gemini", "model_id": "gemini-3.1-flash-lite", ...},
    ...
}

def provider_class(module: str) -> type:
    return getattr(import_module(f"{__package__}.{module}"), PROVIDER_CLASSES[module])

def get_provider(key: str):
    ...
    provider_cls = provider_class(cfg.pop("provider"))
    ...
```

- `__package__` is `"providers"` here, so `import_module("providers.gemini")`
  loads the adapter file, which then imports `google.genai` at its own top.
- `getattr(module, "GeminiProvider")` fetches the class from the module by name.

Test (`tests/test_lazy_imports.py`): runs Python in a **fresh subprocess**,
imports Fox, and checks `sys.modules` contains none of `google.genai`,
`openai`, `anthropic`; then builds one provider class and checks only its SDK
loaded. A subprocess is needed because the test runner process has already
imported the SDKs through other tests, so checking `sys.modules` there would
prove nothing.

### Result

| | Before | After |
|---|---|---|
| `import providers, agent, calls` + vault/routing | 4.5–6.7 s | **0.21–0.25 s** |
| SDKs loaded at import | google.genai, openai | none |

Tests: 264 pass (262 + 2 new).

### Why this way

- **Rejected: move every SDK import inside the adapter methods.** Works, but
  scatters imports across every method, and every call pays a dictionary lookup
  in `sys.modules`. Importing the *adapter file* lazily keeps each adapter
  normal (SDK import at the top) and puts the laziness in one place.
- **Rejected: a pre-started pool of warm workers.** Hides the cost instead of
  removing it, costs memory while idle, and is more machinery. Lazy imports
  fixed the cause in ~10 lines.
- **Strings vs. factory functions (`lambda: ...`) in the registry:** strings
  are plain data, so the registry stays printable and comparable, and tests
  can check every entry names a known module.
- Anthropic was already lazy (`import anthropic` inside `__init__`), which is
  the per-method style; it still works and is unaffected.

### Interview questions

- **Q: What does `import` actually do in Python?** Finds the module, executes
  its top-level code once, stores the module object in `sys.modules`; later
  imports return the cached object.
- **Q: How would you find what makes a Python program slow to start?**
  `python -X importtime`, sort by cumulative time; then lazy-import the heavy
  modules that aren't needed on the start-up path.
- **Q: Why is process start-up more expensive on Windows than Linux?** No
  `fork()`: Windows creates a fresh process (`spawn`) that must re-import
  everything, while a forked Linux child inherits the parent's loaded memory
  (copy-on-write).
- **Q: Trade-off of lazy imports?** The first call that needs the module pays
  the cost (latency moves, it doesn't disappear), and import errors surface at
  call time instead of start-up. Fine here: a session uses one or two
  providers, and a missing SDK already fails clearly when that model is used.
- **Q: How do you test that something is *not* imported?** Check
  `sys.modules` in a clean subprocess, since the test process may already have
  it loaded.

---

## Step 2 — Validating a workspace without installing it

### The problem

`paths.set_workspace()` did two jobs at once: **check** a folder (it exists,
it's a directory, it isn't a drive root or your home folder) and **install** it
as the process-wide global. The hub needs the first job without the second. A
`start_session` can name any folder and the hub must check it, but the hub
never works in a folder itself, so its global must not change.

We also need a way to say "these two paths are the same project". On Windows,
`C:\Users\me\Desktop\shop` and `c:\users\me\desktop\shop\.` are the same folder.
The old database really did contain both spellings.

### The concept

- **Pure function vs. side effect.** A pure function only computes and returns
  a value. A side effect changes state somewhere else, here a module global.
  Splitting "check" (pure) from "install" (side effect) lets each caller take
  only the part it needs, and makes the check easy to test.
- **Path normalisation.** `os.path.normpath` removes `.`, `..` and doubled
  separators. `os.path.normcase` lower-cases and turns `/` into `\`, but **on
  Windows only**. It does nothing on Linux or macOS, where file names really
  are case-sensitive. Together they give one canonical string per folder.

### The code

```python
# loom/paths.py
def validate_workspace(path, allow_unsafe=False) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists(): raise WorkspaceError(...)
    if not resolved.is_dir(): raise WorkspaceError(...)
    if not allow_unsafe: _reject_dangerous(resolved)
    return resolved

def set_workspace(path, allow_unsafe=False) -> Path:
    global _workspace
    _workspace = validate_workspace(path, allow_unsafe)
    return _workspace

def workspace_key(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))
```

`set_workspace` keeps its old behaviour by calling the new function, so no
existing caller had to change. Tests: `tests/test_paths.py`.

### Interview questions

- **Q: Why separate validation from mutation?** Reuse: the hub only validates,
  while the worker validates *and* installs. Testability: there's no global to
  reset between tests. It's the "functional core, imperative shell" idea in
  miniature.
- **Q: How do you compare file paths correctly across operating systems?**
  Resolve both, then compare a normalised form. `normcase` handles Windows
  case-insensitivity. On POSIX, two paths that differ only in case really are
  different folders, so they must stay different.

---

## Step 3 — The store: workers don't migrate, and projects come from SQL

### The problem

1. **Migrations.** Until now, one process opened `fox.db` and upgraded its
   schema if needed. Now several processes open it. If two workers started at
   the same moment against an old database, both could try to migrate.
2. **Projects.** The sidebar needs "the 5 most recent projects, and the full
   history on request", where a project is a folder. Every session row already
   records its workspace. But grouping by the raw path splits one folder into
   two projects when its spelling differs.

### The concept

- **Schema migrations** are numbered upgrade steps that run in order. SQLite
  stores the current number in `PRAGMA user_version`. On open, you run every
  step after that number, each in its own transaction, then bump the number.
- **A single migrator.** The simplest way to stop two processes migrating at
  once is to let only one process ever migrate. The hub starts first and
  migrates. Workers check the version and refuse to run if it's wrong. A file
  lock or `BEGIN EXCLUSIVE` around the migration would also work, but adds
  moving parts we don't need.
- **Why the backfill is in Python.** SQLite has `lower()` but no `normcase` or
  `normpath`, so computing `workspace_key` for existing rows has to happen in
  Python. A migration can now be either a SQL script or a Python function that
  receives the connection. Both run inside one transaction.
- **A composite index** on `(workspace_key, started_at)` lets "sessions of this
  project, newest first" read straight from the index, instead of scanning and
  sorting the whole table.
- **WAL mode with several writers.** In WAL (write-ahead log) mode, readers
  never block writers and writers never block readers. Writers still take
  turns. `BEGIN IMMEDIATE` takes the write lock up front, and
  `busy_timeout = 5000` makes a second writer wait up to 5 s instead of
  failing. That's why several worker processes can safely append to the same
  database.

### The code

```python
# loom/store/migrations.py
def _add_workspace_key(db):
    db.execute("ALTER TABLE sessions ADD COLUMN workspace_key TEXT")
    rows = db.execute("SELECT id, workspace FROM sessions").fetchall()
    db.executemany("UPDATE sessions SET workspace_key = ? WHERE id = ?",
                   [(workspace_key(w), sid) for sid, w in rows])
    db.execute("CREATE INDEX sessions_by_project ON sessions (workspace_key, started_at)")

MIGRATIONS = [schema.sql, model_calls.sql, _add_workspace_key]   # version 3

def _run(db, number, step):
    if callable(step):
        if db.in_transaction: db.commit()
        db.execute("BEGIN"); step(db)
        db.execute(f"PRAGMA user_version = {number}"); db.execute("COMMIT")
    else:
        db.executescript(f"BEGIN;\n{step}\nPRAGMA user_version = {number};\nCOMMIT;")
```

```python
# loom/store/__init__.py
Store.open(path, readonly=False, migrate=True)
#   migrate=False (workers): version != len(MIGRATIONS) → StoreError, nothing touched

def projects(self, limit=None):
    SELECT workspace_key,
           (SELECT workspace FROM sessions latest
             WHERE latest.workspace_key = s.workspace_key
             ORDER BY started_at DESC LIMIT 1)                AS path,   -- newest spelling
           COUNT(*)                                            AS sessions,
           SUM(status = 'running')                             AS running,
           MAX(MAX(started_at, COALESCE(ended_at, started_at))) AS last_active
    FROM sessions s WHERE parent_id IS NULL
    GROUP BY workspace_key ORDER BY last_active DESC LIMIT ?          -- -1 = no limit

def sessions(self, project=None, limit=None):   # newest first, optional folder filter
```

The `session_started` projection now fills in `workspace_key` for every new row.

SQL details worth remembering:
- `MAX(a, b)` with two arguments is a **scalar** function that returns the
  larger value. `MAX(x)` with one argument is the **aggregate** over the group.
  So the nested form means "the later of start and end for each row, then the
  latest of those across rows".
- `SUM(status = 'running')` works because a comparison evaluates to `1` or `0`
  in SQLite.
- `LIMIT -1` means "no limit", so one query serves both the sidebar
  (`limit=5`) and the full history (`limit=None`).
- The `path` column is a **correlated subquery**. It runs once per group and
  picks the most recent spelling of the folder to display.
- Side chats (`parent_id IS NOT NULL`) aren't counted as separate sessions of
  a project. They belong to their parent.

**Tests:**
- `tests/test_migrations.py`: a v2 database with both `Desktop` and `desktop`
  spellings collapses to one key after migrating.
- `tests/test_store.py`:
  - `migrate=False` refuses an old database and leaves it at its version;
  - projects merge spellings, sort by last activity and honour `limit`;
  - sessions sort and filter correctly.

The tests pin the clock, because two events in the same millisecond would
otherwise tie in the ordering.

Checked on a **copy** of the real `fox.db`:
- it upgraded from v2 to v3;
- no row was left without a key;
- `integrity_check` returned ok;
- it found two projects, `fox-fixture` and `Torvec`.

### Interview questions

- **Q: Several processes share one SQLite file. What can go wrong, and how do
  you prevent it?** Two things:
  - concurrent writers, solved by WAL plus `BEGIN IMMEDIATE` plus a busy
    timeout;
  - concurrent schema changes, solved by a single migrator, with every other
    process checking the version and refusing to start.
- **Q: When would you store a computed column instead of computing it in each
  query?** When the database can't compute it (SQLite has no `normcase`), or
  when you need to index it. Both are true here.
- **Q: How do you add a column to a table that already has data?** `ALTER
  TABLE ADD COLUMN` (nullable), backfill it in the same migration, then index
  it. Do all of it in one transaction, so a failure leaves the old version
  intact.
- **Q: What does `PRAGMA user_version` give you?** A free integer in the
  database header. It's the cheapest way to version a schema.

---

## Step 4 — API keys across processes

### The problem

Keys live in `FOX_HOME/.env`, and each process loads them into its own
`os.environ` at start-up. **Environment variables are per process.** When the
hub saves a new key, its own `os.environ` changes, but a worker that's already
running keeps the old value forever. And even once a worker's environment is
updated, its provider object was built with the old key and keeps using it.

### The concept

- A process's environment is copied from its parent **at creation**, and is
  private after that. You can't push a variable into another running process.
  You have to tell it to re-read a shared source, here the `.env` file.
- **Cache invalidation.** A key has three cached copies:
  - `.env`, the source of truth;
  - `os.environ`, one per process;
  - `provider.api_key`, one per provider object.

  Each copy needs a rule for when it gets refreshed.

### What we changed

| Copy | Refreshed when |
|---|---|
| `.env` | `vault.set_key` writes it (in the hub) |
| worker `os.environ` | The hub sends `reload_keys`. The worker runs `vault.reload()`, which does `load_dotenv(override=True)` and clears the validation cache. |
| `provider.api_key` | Before every model call, `calls.current_provider()` compares it with `os.environ`, and rebuilds the provider if they differ. |

Also:
- `vault.get_key()` and `is_present()` read `.env` first, then `os.environ`,
  so a fresh value always wins, even before a reload arrives.
- `vault.remember(validated)`: the hub sends the key checks it already knows
  along with `start_session`.
  - **Finding:** when an orchestrator session has no model given,
    `routing.pick_orchestrator()` calls `validate_key` on each strong model.
    That's a network round-trip whenever the cache is empty, and every new
    worker starts with an empty cache. Seeding the cache from the hub avoids
    the round-trip.
  - `pick_executor()` only checks that a key is present, so it's unaffected.

```python
# loom/calls.py
def current_provider(provider, model_key):
    env_key = MODEL_REGISTRY.get(model_key, {}).get("env_key")
    fresh = os.environ.get(env_key) if env_key else None
    if fresh and getattr(provider, "api_key", fresh) != fresh:
        return get_provider(model_key)
    return provider
# generate_or_pause() calls it at the top of every attempt
```

`getattr(provider, "api_key", fresh)` has two effects:
- test doubles, which have no `api_key`, are never rebuilt;
- a key that was removed (`fresh` is empty) doesn't throw away a working
  provider.

Tests (`tests/test_keys.py`):
- a value written to `.env` by another process wins;
- `reload()` updates the environment and clears the validation cache;
- `remember` ignores unknown models;
- a provider with a stale key is rebuilt, and a current one is kept.

### Interview questions

- **Q: How do you change configuration in a running process?** Either restart
  it, or have it re-read a shared source when signalled. Here the signal is a
  `reload_keys` message. On Unix, `SIGHUP` is the classic "reload your config"
  signal.
- **Q: Where can a secret be cached, and how do you make key rotation work?**
  At the source, in the process environment, and inside client objects.
  Rotation only works if every layer has a refresh rule. Here, the client's key
  is compared with the environment before each call.

---

## Step 5 — `awaiting_input`: knowing when a session is idle

### The problem

A sidebar has to show each session as *running*, *idle* (waiting for you),
*needs approval*, *done* or *failed*. The hub can't look inside a worker, so it
can only know a session is idle if the worker says so.

### What we changed

- New event `awaiting_input {task_id: null}` in `loom/events.py`.
- It's emitted right before each of the two places where a top-level loop
  blocks on `injection_queue.wait_for_message(None)`:
  - the keep-alive branch of `agent.run_agent`;
  - the "no tasks running" branch of `run_orchestrator`.
- The hub turns events into a status (`server.session_status()`):

| Event | Status |
|---|---|
| `session_ended` | its own `status` (`done` / `failed`) |
| `approval_requested` | `needs_approval` |
| top-level `awaiting_input` or `model_error` | `idle` |
| anything else | `running` |

`model_error` counts as idle too: the session is paused until you send a
message. That's the pause-on-model-error behaviour from the TUI branch.

The hub broadcasts `session_status` to **every** connected client, but only
when the status changes. A sidebar sees sessions it didn't start without being
flooded.

Tests: the existing follow-up tests now check the order `awaiting_input →
human_message_injected → awaiting_input`, for both the agent and the
orchestrator.

### Interview questions

- **Q: How does a supervisor know the state of a process it can't inspect?**
  Either the process reports it as an event (here, an explicit "I'm waiting"
  event), or the supervisor infers it. Explicit events are cheaper and exact.
- **Q: Why broadcast only on change?** Status is a current state, not a log.
  Sending only the transitions keeps the traffic in proportion to real changes.

---

## Step 6 — The worker process (`loom/worker.py`)

### The problem

The code that ran a session lived inside `server.py`, as a thread. It has to
move into its own OS process that the hub can start, talk to and kill.

### The concept

- **Process vs. thread.**
  - Threads share one memory space. They're cheap to create and to share data
    between, but a crash or memory leak in one affects them all.
  - Processes have separate memory. A crash stays inside one process, the OS
    can kill one cleanly, and each has its own globals, like the workspace. The
    cost is start-up time (Step 1), and having to send data between them
    explicitly.
- **IPC over pipes.** When a parent starts a child with `stdin=PIPE,
  stdout=PIPE`, the OS gives them two one-way byte streams. We send **JSON
  lines**: one JSON object per line, ending in `\n`. They're easy to frame
  (read until newline), easy to debug (they're text), and need no extra
  library.
- **File descriptors.** 0, 1 and 2 are stdin, stdout and stderr.
  `os.dup(fd)` makes a second descriptor for the same file. `os.dup2(a, b)`
  makes `b` point wherever `a` points.

### Keeping stdout clean (the easy thing to get wrong)

stdout now carries the protocol. A single stray `print()` anywhere in agent
code (there are many, e.g. "Rate limited, waiting…") would inject a non-JSON
line. These are the first lines of `worker.py`, run before any Fox module is
imported:

```python
_PROTOCOL = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1, newline="\n")  # private copy of the real stdout
_COMMANDS = os.fdopen(os.dup(0), "rb")                                               # private copy of stdin
os.dup2(2, 1)               # fd 1 now IS stderr: C code and child processes can't reach the protocol
sys.stdout = sys.stderr     # Python-level print() goes to stderr too
```

stderr is a log file the hub opened (`logs/worker-<id>.log`), so prints and
tracebacks land there. Every protocol write goes through one function that
holds a `threading.Lock`. Executor threads emit events at the same time, and
two half-written lines interleaved would corrupt both.

### A real bug we hit: crash on exit (0xC0000005)

The first version read commands from `sys.stdin.buffer` on a daemon thread.
Every test passed, except that the worker **crashed while exiting** with
Windows code `3221225477` (`0xC0000005`, access violation). Running it with
`python -X faulthandler` showed the cause:

```
Fatal Python error: _enter_buffered_busy: could not acquire lock for
<_io.BufferedReader name='<stdin>'> at interpreter shutdown, possibly due to daemon threads
```

At shutdown, Python flushes and closes `sys.stdin`, which needs the buffer's
lock. But the daemon thread was blocked inside `readline()` *holding* that
lock, so the interpreter aborted.

**Fix:** the reader uses its own duplicated descriptor (`_COMMANDS`). That's a
different buffer object, which Python's shutdown never touches.

**Lesson:** daemon threads blocked in I/O on the standard streams don't mix
with interpreter shutdown.

### Lifecycle

```
start  → paths.set_workspace(--workspace) → Store.open(--db, migrate=False)
       → reader thread on stdin → write {"kind":"ready","pid"}
run    → each start_session: Session (InjectionQueue, ApprovalGate, Memory), agent/orchestrator thread
       → every event: Store.append_event → write {"kind":"event","event":{session_id, seq, type, …}}
exit   → when every hosted session has ended, or on shutdown / stdin EOF
       → sessions still running end as failed "interrupted: server stopped"
       → wait up to 10 s for threads → {"kind":"exiting","code":0} → exit 0
```

- **Side chats** run in their parent's worker and share its `Memory` object.
  That's also required because `Store.next_note_id` counts and then inserts,
  which is only safe inside one process.
- **The worker writes its own events to the database** before sending them, so
  history survives a hub crash. The `seq` it sends is the one the database
  assigned.
- **`sandboxd`** is started lazily by `sandbox_client.py` with `cwd` set to the
  worker's workspace. So each worker gets its own `sandboxd`, in the right
  folder, with no code change.
  - Verified: hard-killing the parent with `taskkill /F` makes `sandboxd` see
    stdin EOF and exit immediately.
  - If it's in the middle of a command, it finishes that command first (30 s
    timeout at most).

### Measured

The real `worker.py` takes **0.29–0.31 s** from spawn to `ready`, against a
1.5 s target.

**Tests** (`tests/test_worker.py`) run the real worker through
`tests/scripted_worker.py`. That script swaps in a scripted model, which also
prints to stdout and writes to fd 1. They cover:
- a whole session over the pipe: `session_started → model_called → agent_turn
  → awaiting_input`, then a follow-up, then `end_session`, then exit 0, with
  the rows in the database;
- prints never reach the protocol, but do reach the log;
- closing stdin ends sessions as interrupted;
- bad lines and unknown sessions give errors, not crashes;
- a side chat runs in the same process;
- `ready` arrives quickly.

### Interview questions

- **Q: Threads or processes for isolating unstable or untrusted work?**
  Processes. They have separate memory, a crash stays local, the OS can kill
  one, and per-process globals (like the workspace) stop being shared. Threads
  are for cheap concurrency inside one trust boundary.
- **Q: How do you frame messages on a byte stream?** Either with delimiters
  (newlines, with JSON escaping any newlines inside strings) or with a length
  prefix. JSON lines are the simplest when payloads are text.
- **Q: Your child process's stdout is the protocol. How do you stop libraries
  printing into it?** Duplicate the real stdout for the protocol, then point
  fd 1 at stderr with `dup2`. That way even C-level writes and subprocesses
  can't reach it.
- **Q: Why lock around writes from several threads?** One logical message
  takes several `write` calls. Without a lock, two threads' lines can
  interleave.
- **Q: What's a daemon thread, and what's the catch?** A thread that doesn't
  keep the process alive. At exit it's frozen wherever it is, holding whatever
  locks it holds. That's exactly what crashed us.

---

## Step 7 — The hub (`loom/server.py`)

### The problem

`server.py` ran agents itself. It now has to be a **router and supervisor**:
- start one worker per session;
- route each client command to the right worker;
- relay each worker's events to the right clients;
- notice when a worker dies;
- answer the questions that apply to the whole machine.

And it must not import any agent code.

### The concept

- **The supervisor pattern** (as in Erlang/OTP, systemd or Kubernetes). A
  small, stable process owns the lifecycle of workers that are allowed to
  fail. It watches each one, records what happened when one dies, and keeps
  serving everyone else.
- **asyncio.** The hub is one thread running an event loop. Every client
  connection and every worker's stdout is a coroutine that `await`s I/O, so
  thousands of idle connections cost nothing. The rule is: never block the
  loop. Anything slow, like a key validation network call, runs in
  `asyncio.to_thread`.
- **Pub/sub.** Clients subscribe to session ids, via `start_session` or
  `attach_session`. Events are published to that session's subscribers.
  `session_status` goes to every client.

### The pieces

```python
class SessionInfo:     session_id, parent_id, workspace, task, mode, permission_mode, model_key, status
class WorkerHandle:    process, sessions (hosted ids), ended, alive, reason,
                       ready (asyncio.Event), exited (asyncio.Event), write_lock (asyncio.Lock)
class Hub:             workers   {session_id → WorkerHandle}
                       sessions  {session_id → SessionInfo}
                       subscribers {session_id → set(ws)},  clients set(ws)
```

### Starting a session

```
client ── start_session ──▶ hub
                            validate permission_mode, workspace (paths.validate_workspace)
                            new uuid, SessionInfo, WorkerHandle, subscribe the client
client ◀── session_started {session_id, workspace} ── (immediately)
all    ◀── session_status running
                            background task:
                              create_subprocess_exec(python worker.py --workspace … --db …,
                                                     stdin/stdout=PIPE, stderr=log file, cwd=workspace,
                                                     limit=64 MiB)
                              wait for {"kind":"ready"} (30 s timeout, junk lines skipped)
                              write start_session line → ready.set() → start the relay task
```

- **Reply first, spawn in the background.** The hub generates the id, so it
  can answer at once, and nothing waits on process start-up.
- **Commands sent before the worker is ready** wait on `worker.ready`, then on
  `write_lock`.
  - The `start_session` line is written before `ready` is set, so it's always
    the first line the worker sees.
  - `asyncio.Lock` is first-in first-out, so later commands keep their order.
- **`limit=64 MiB`.** asyncio's `StreamReader` rejects lines longer than its
  limit (64 KiB by default) with `LimitOverrunError`. A single `tool_result`
  that holds a whole file is easily bigger than that. Tested with a 2 MiB
  event.

### The relay and failure handling

```python
async def _relay(worker):
    while line := await worker.process.stdout.readline():
        message = json.loads(line)             # non-JSON lines are skipped
        if message["kind"] == "event": await self._on_event(message["event"])
        elif message["kind"] == "error": forward to that session's watchers
    await self._worker_exited(worker, await worker.process.wait())
```

- `_on_event` does two things:
  - it sends the event to the session's subscribers with
    `websockets.broadcast`, which queues the frame on each connection without
    waiting, so one slow client can't stall the relay;
  - it updates the session's status.
- `_worker_exited` handles a dead worker:
  - for every session that worker hosted with no `session_ended`, the hub
    appends `session_ended {failed, "worker exited with code N"}` (or
    `"killed"`) to the database and broadcasts it;
  - other workers are untouched;
  - measured: **0.11 s** from `taskkill /F` to the client seeing it.
- **A foreign-key detail.**
  - `events.session_id` references `sessions(id)`.
  - If a worker dies before writing `session_started`, there's no session row,
    and writing `session_ended` would fail the constraint.
  - So `_record_failure` writes `session_started` itself, from the
    `SessionInfo` it kept, when the row is missing.
- **A worker that fails to start** (it exits, or sends no `ready` within
  30 s):
  - it's killed;
  - the client gets an `error`;
  - the session ends `failed` with `"worker failed to start: <reason>"`, plus
    the last 20 lines of the worker's log.

### Other commands

| Command | Hub does |
|---|---|
| `attach_session` | Subscribes first, then reads `session_view` from its own database connection. The client drops duplicates by `seq`. Subscribing after reading could lose events. |
| `human_message` / `approval_response` / `end_session` | Forwards to the owning worker. An unknown or ended session gets an `error`, as before. |
| `kill_session` | Calls `process.kill()` (TerminateProcess on Windows), waits for the exit to be recorded as `failed / killed`, and replies `kill_session_done`. |
| `list_models` / `set_model_key` | Stay in the hub. After saving a key, the hub sends `reload_keys` to every live worker. |
| `list_projects {limit?}` | Returns `Store.projects(limit)` plus `current`, the hub's default folder, to pin at the top. |
| `list_sessions {project?, limit?}` | Returns `Store.sessions(...)` plus a `live` flag per row. |

### Start-up and shutdown

- **Start-up:**
  - open the database **with** migration (the hub is the only migrator);
  - run `mark_interrupted()`: every session still `running` gets
    `session_ended {failed, "interrupted: server restarted"}`;
  - at start-up no worker can be alive, so any `running` row is left over from
    a crash. Phase 4 will resume these sessions instead.
- **Shutdown (Ctrl+C):** send `shutdown` to every worker, wait up to 10 s, then
  kill the rest. If the hub dies without doing that, each worker sees stdin EOF
  and shuts itself down.

### The import rule

The hub imports `paths`, `permissions`, `config.vault` and `store`, and no
agent code or SDK. A test checks this in a clean subprocess.

One caveat: `store`'s projections import `orchestration.tasks` for its status
enum. That's a data definition, not agent code.

### Interview questions

- **Q: Design a system where one bad job can't take down the others.** Use a
  supervisor plus one process per job, talking over pipes:
  - the supervisor watches each child's exit and records it;
  - children exit when the supervisor's pipe closes, so nothing is orphaned.
- **Q: How do you detect that a child process died?** Its stdout reaches EOF,
  and `wait()` returns its exit code. A local child needs no polling or
  heartbeats.
- **Q: A client subscribes to a stream and also wants the history. How do you
  avoid gaps?** Subscribe first, then read the history, and drop duplicates
  using a per-stream sequence number.
- **Q: Why `websockets.broadcast` instead of `await ws.send` in a loop?** A loop
  waits on each client in turn, so one slow client slows everyone and blocks
  the relay. Broadcast queues the frame on each connection and returns.
- **Q: What happens to running work when the supervisor restarts?** Here:
  - workers exit on EOF and record "interrupted";
  - anything that couldn't record it (hub and workers killed together) is found
    by the start-up sweep;
  - the next step would be resuming from the event log (Phase 4).

---

## Step 8 — Tests

| File | What it covers |
|---|---|
| `tests/fake_worker.py` | A stand-in worker that speaks the protocol and writes to the database. It echoes messages, crashes on `"crash"`, sends a 2 MiB line on `"big"`, and writes junk lines. Through the env var `FAKE_WORKER` it can also refuse to get ready, or die at start. |
| `tests/test_hub.py` (16 tests) | A real hub on a random port with real WebSocket clients:<br>• the start reply has `workspace`<br>• events carry `seq` and reach only watchers, while `session_status` reaches everyone<br>• two workers, with routing by session<br>• a side chat runs in the parent's process (same pid)<br>• bad workspaces and unknown ids give errors<br>• ending a session makes its worker exit<br>• a crash fails only its own session<br>• never-ready and die-at-start give a failed start with the log tail<br>• kill<br>• the start-up sweep<br>• shutdown<br>• a 2 MiB event and junk lines<br>• `set_model_key` → `reload_keys`<br>• projects merge spellings, and sessions report `live`<br>• the hub imports no agent code |
| `tests/scripted_worker.py` + `tests/test_worker.py` (6 tests) | The real worker with a scripted model (Step 6). |
| `tests/test_lazy_imports.py`, `test_paths.py`, `test_keys.py`, the store and migration tests | Steps 1–4. |

Totals: **302 Python tests pass**, up from 262 before this branch, and
`go test ./...` passes in `tui/`. No test uses a real model or API credits.

---

## Step 9 — Exit checks

These were run on this machine with the **real hub, real worker processes,
real pipes, real WebSocket and real `taskkill`**. The model was the scripted
one, so no API credits were spent. The two projects were `fox-fixture` and a
copy of it, `fox-fixture-2`.

| # | Check | Result |
|---|---|---|
| 1 | Tests | ✅ 302 Python, plus Go |
| 2 | Two projects at once | ✅ Both sessions ran, in 2 worker processes. `list_projects` returned `fox-fixture-2` (1 running) and `fox-fixture` (1 running). |
| 3 | Crash isolation | ✅ `taskkill /F` on one worker ended its session `failed: "worker exited with code 1"` in **0.11 s**. The other session still answered. No `sandboxd` was left orphaned (separate check, Step 6). |
| 4 | Start latency | ✅ `ready` in **0.30 s**. `start_session` to the worker's first event took **0.33 s**. |
| 5 | Hub restart | ✅ Two cases:<br>• Hub killed: **0** workers were left after 2 s. All exited on stdin EOF and recorded `interrupted: server stopped`. The new hub's `attach_session` replayed the full history.<br>• Hub and workers killed together (`taskkill /T`): the new hub marked the session `interrupted: server restarted`. |
| 6 | Keys | ✅ In unit tests: reload, provider rebuild, and `reload_keys` reaching live workers.<br>⬜ A live free-model run is waiting for Anchit's OK. |
| 7 | TUI unchanged | ✅ `go test` passes. The only TUI change is ignoring the new broadcast types (below).<br>⬜ An interactive TUI run is waiting for Anchit. |

### The one TUI change

The TUI's state store creates a session for any frame that has a `session_id`.
So a `session_status` for a session started elsewhere would have added empty
sessions to the sidebar. Now `state.Apply` returns early for `session_status`,
`projects` and `sessions`, until the projects sidebar uses them. This adds new
constants in `internal/protocol/event.go` and the test
`TestStatusOfAnotherSessionAddsNothing`.

---

## The final architecture

```
                    ┌────────────────────────── TUI (Go) ──────────────────────────┐
                    │ one WebSocket: commands out, events + replies + statuses in  │
                    └──────────────────────────────┬────────────────────────────────┘
                                                   │ ws://localhost:8765
┌──────────────────────────────── hub: loom/server.py (asyncio, 1 thread) ─────────────────────────────┐
│ routes commands to workers · relays events to subscribers · broadcasts session_status to everyone    │
│ vault (keys), list_projects, list_sessions · migrates fox.db at start · marks orphans interrupted     │
│ watches every worker's exit · Ctrl+C → shutdown every worker                                         │
└──────┬───────────────────────────────┬────────────────────────────────┬──────────────────────────────┘
       │ stdin: commands (JSON lines)  │                                │
       │ stdout: events (JSON lines)   │                                │
       │ stderr: logs/worker-<id>.log  │                                │
┌──────▼──────────────┐       ┌────────▼────────────┐          ┌────────▼────────────┐
│ worker (root A)     │       │ worker (root B)     │          │ worker (root C)     │
│ workspace: shop     │       │ workspace: shop     │          │ workspace: api      │
│ A + its side chats  │       │ B                   │          │ C                   │
│ orchestrator thread │       │ agent thread        │          │ …                   │
│  └ executor threads │       │                     │          │                     │
│ sandboxd (cwd=shop) │       │ sandboxd (cwd=shop) │          │ sandboxd (cwd=api)  │
└──────┬──────────────┘       └────────┬────────────┘          └────────┬────────────┘
       └─────────── append_event ──────┴──────────── fox.db (SQLite, WAL) ┘◀── hub reads (attach, lists)
```

**Who owns what**

| Thing | Owner |
|---|---|
| session ids | the hub (uuid4) |
| the event log | each worker writes its own sessions; the hub writes only failures and the start-up sweep |
| workspace | each worker, set once at start |
| `InjectionQueue`, `ApprovalGate`, `Memory`, claims, executors | the worker |
| keys (`.env`) | written by the hub, re-read by workers on `reload_keys` |
| schema migrations | the hub only |
| session status (running / idle / needs_approval / done / failed) | the hub, derived from events |

**A message's round trip**

```
TUI: human_message ──▶ hub: find worker for session_id ──▶ worker stdin: {"kind":"human_message",…}
     worker: InjectionQueue.push → agent loop wakes → model call
     worker: append_event(agent_turn) → seq 12 → stdout {"kind":"event","event":{…,"seq":12}}
hub: relay → subscribers of that session ◀── TUI renders it (drops it if seq ≤ last seen)
```

## Protocol reference

### TUI → hub (WebSocket)

| Command | Fields | Reply |
|---|---|---|
| `start_session` | `task, model_key?, mode?, permission_mode?, parent_session_id?, workspace?` | `session_started {session_id, workspace}` |
| `attach_session` | `session_id` | `attached {session_id, state}` |
| `human_message` | `session_id, task_id?, content` | `human_message_queued` |
| `approval_response` | `session_id, request_id, approved, always?, tool_name?` | — |
| `end_session` | `session_id, task_id?` | `end_session_queued` |
| `kill_session` | `session_id` | `kill_session_done` |
| `list_models` | `validate?` | `models` |
| `set_model_key` | `model_key, value` | `models` |
| `list_projects` | `limit?` | `projects {current, projects:[{path, name, sessions, running, last_active}]}` |
| `list_sessions` | `project?, limit?` | `sessions {sessions:[{session_id, parent_id, workspace, task, mode, permission_mode, model_key, status, started_at, ended_at, live}]}` |

### Hub → TUI, unprompted

- Every session event, as `{session_id, seq, type, …payload}`. The catalog is
  in `loom/events.py`; the new event is `awaiting_input`.
- `session_status {session_id, parent_id, workspace, status, model_key, mode}`,
  sent to every client when a status changes.

### Hub ↔ worker (pipes, JSON lines)

| Hub → worker (stdin) | Worker → hub (stdout) |
|---|---|
| `start_session {session_id, task, model_key, mode, permission_mode, parent_session_id, validated}` | `ready {pid}` (once) |
| `human_message {session_id, task_id, content}` | `event {event: {session_id, seq, type, …payload}}` |
| `approval_response {session_id, request_id, approved, always, tool_name}` | `error {session_id?, message}` |
| `end_session {session_id, task_id}` | `exiting {code}` |
| `reload_keys {}` · `shutdown {}` · EOF counts as shutdown | |

## Where we changed PIVOT_A, and why

1. **Worker events are nested**, as `{"kind":"event","event":{…}}` instead of
   `{"kind":"event", …payload}`. The `model_error` and `memory_written`
   payloads have their own `kind` field. Flattened, it would overwrite the
   protocol's `kind` and turn those events into garbage.
2. **Shutdown and stdin EOF end sessions as `failed / "interrupted: server
   stopped"`**, not `done`. The user didn't finish those sessions; the server
   went away. Recording them as `done` would hide them from Phase 4's resume.
   `end_session` still gives `done`.
3. **`list_projects` takes `limit` and returns `current`.** This supports the
   sidebar rule: the current project, then the 5 most recent, with the full
   history on request.
4. **`start_session` to the worker carries `validated`**, so a new worker
   doesn't re-check keys over the network (see Step 4).
5. **The worker takes `--db`** (default `FOX_HOME/fox.db`), so tests can use a
   temporary database.
6. **A top-level `model_error` also counts as idle**, because the session is
   paused waiting for you.
7. **The hub writes `session_started` itself** when a worker died before
   writing it, to satisfy the foreign key (Step 7).
8. **The TUI ignores `session_status`, `projects` and `sessions`** for now
   (Step 9).

## Known limits

- **A side chat can't be opened on a finished session**, because that
  session's worker has exited. You get `unknown parent_session_id`. Before
  this branch, the server kept every session object forever. Resuming is
  Phase 4.
- **The first Gemini call in each worker pays for the SDK import** (≈3–5 s).
  It happens once per worker, and only in sessions that use Gemini.
- **`sandboxd` finishes a running command** (30 s at most) before it notices
  EOF.
- **The hub's database calls are synchronous** on the event loop. They take
  milliseconds, but under heavy write contention one could wait up to
  `busy_timeout` (5 s), and the hub would stall for that long.
- **Commands sent before a worker is ready wait for it.** If a worker hangs at
  start, that one client's command loop pauses for up to 30 s.
- **`preferences.py`** writes its file with a thread lock but no cross-process
  lock. Writes are rare, so it's left as is.
- **Timestamps have millisecond resolution**, so two sessions started in the
  same millisecond tie in "newest first" ordering.
- **The first start of the new hub will mark 8 old `running` sessions** in the
  real `fox.db` as interrupted. They're leftovers from earlier runs.
