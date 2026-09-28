-- Run records. Postgres loads this file the first time the database starts.

-- One row per requested task. The ID and status are what the client sees.
CREATE TABLE runs (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued, running, finished, failed
    report_path  TEXT,                            -- where the saved report lives
    check_result JSONB,                           -- whether the report passed the check, and the evidence
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per execution of a run. Demo 1 only ever makes attempt 1.
CREATE TABLE attempts (
    run_id        TEXT NOT NULL REFERENCES runs (id),
    number        INT  NOT NULL,
    worker_id     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',  -- running, finished, failed
    turns         INT,
    input_tokens  INT,
    output_tokens INT,
    cost_usd      DOUBLE PRECISION,
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    PRIMARY KEY (run_id, number)
);

-- What happened, in order: submission, claim, each agent step, the check.
CREATE TABLE events (
    id      BIGSERIAL PRIMARY KEY,
    run_id  TEXT NOT NULL REFERENCES runs (id),
    attempt INT,
    type    TEXT NOT NULL,
    detail  JSONB NOT NULL DEFAULT '{}',
    at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
