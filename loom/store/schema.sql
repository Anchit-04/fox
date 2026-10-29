CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    parent_id       TEXT REFERENCES sessions(id),
    workspace       TEXT NOT NULL,
    task            TEXT NOT NULL,
    mode            TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    model_key       TEXT,
    status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    final_text      TEXT,
    todos           TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL,
    ended_at        TEXT
) WITHOUT ROWID;

CREATE TABLE events (
    session_id TEXT NOT NULL REFERENCES sessions(id) DEFERRABLE INITIALLY DEFERRED,
    seq        INTEGER NOT NULL,
    ts         TEXT NOT NULL,
    task_id    TEXT,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL CHECK (json_valid(payload)),
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;

CREATE INDEX events_by_type ON events (session_id, type);
CREATE INDEX events_by_task ON events (session_id, task_id);

CREATE TABLE tasks (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    task_id     TEXT NOT NULL,
    description TEXT NOT NULL,
    lane        TEXT NOT NULL CHECK (lane IN ('read', 'write')),
    scope       TEXT NOT NULL CHECK (json_valid(scope)),
    model_key   TEXT,
    status      TEXT NOT NULL CHECK (status IN ('queued', 'running', 'needs-approval', 'done',
                                                'failed', 'cancelled', 'escalated')),
    report      TEXT CHECK (report IS NULL OR json_valid(report)),
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    ended_at    TEXT,
    PRIMARY KEY (session_id, task_id)
) WITHOUT ROWID;

CREATE INDEX tasks_by_status ON tasks (status);

CREATE TABLE memory_entries (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    entry_id   TEXT NOT NULL,
    task_id    TEXT,
    agent      TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('note', 'task_result')),
    scope      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(scope)),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, entry_id)
) WITHOUT ROWID;
