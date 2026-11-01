CREATE TABLE model_calls (
    session_id         TEXT NOT NULL REFERENCES sessions(id),
    seq                INTEGER NOT NULL,
    task_id            TEXT,
    model_key          TEXT NOT NULL,
    duration_ms        INTEGER NOT NULL CHECK (duration_ms >= 0),
    input_tokens       INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens      INTEGER NOT NULL CHECK (output_tokens >= 0),
    cache_read_tokens  INTEGER NOT NULL CHECK (cache_read_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL CHECK (cache_write_tokens >= 0),
    cost               REAL CHECK (cost IS NULL OR cost >= 0),
    cost_source        TEXT CHECK (cost_source IN ('provider', 'catalog')),
    prefix             TEXT NOT NULL CHECK (json_valid(prefix)),
    ended_at           TEXT NOT NULL,
    PRIMARY KEY (session_id, seq),
    CHECK ((cost IS NULL) = (cost_source IS NULL))
) WITHOUT ROWID;

CREATE INDEX model_calls_by_task ON model_calls (session_id, task_id);
