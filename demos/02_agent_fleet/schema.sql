-- Run records for the fleet. Postgres loads this file the first time the database starts.
-- New since Demo 1: leases on attempts, a workers table, fleet settings, and event sources.

-- One row per requested task. The ID and status are what the client sees.
CREATE TABLE runs (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued, running, accepted, failed
    report_path  TEXT,                            -- where the saved report lives
    check_result JSONB,                           -- the decision and its evidence
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per execution of a run. A run can now have more than one.
CREATE TABLE attempts (
    run_id           TEXT NOT NULL REFERENCES runs (id),
    number           INT  NOT NULL,
    worker_id        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running',  -- running, finished, failed, lost
    lease_expires_at TIMESTAMPTZ NOT NULL,             -- the worker renews this while it runs
    message_id       TEXT NOT NULL,                    -- the queue message this attempt came from
    turns            INT,
    input_tokens     INT,
    output_tokens    INT,
    cost_usd         DOUBLE PRECISION,
    error            TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    PRIMARY KEY (run_id, number)
);

-- One row per worker. The worker writes status and heartbeat. The controller writes the rest.
CREATE TABLE workers (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'ready',   -- ready, busy, waiting, gone
    current_run  TEXT,
    runs_done    INT NOT NULL DEFAULT 0,
    drain        BOOLEAN NOT NULL DEFAULT false,  -- finish the run in hand, then exit
    command      TEXT,                            -- kill, pause, resume: requested on the dashboard
    container    TEXT,                            -- what Docker says: running, paused, exited
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The fleet's targets and limits. The dashboard edits them. The controller and workers read them.
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value DOUBLE PRECISION NOT NULL
);
INSERT INTO settings VALUES
    ('desired_workers', 2),
    ('max_active_runs', 8),
    ('max_fleet_spend_usd', 5),
    ('next_worker', 1);

-- What happened, in order, and who said so: the api, the controller, or a worker.
CREATE TABLE events (
    id      BIGSERIAL PRIMARY KEY,
    run_id  TEXT REFERENCES runs (id),
    attempt INT,
    source  TEXT NOT NULL,
    type    TEXT NOT NULL,
    detail  JSONB NOT NULL DEFAULT '{}',
    at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
