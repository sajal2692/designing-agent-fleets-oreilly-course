"""Demo 2 dashboard: one page that shows the fleet, and a few controls. It only reads and
writes Postgres and Redis. Anything that touches Docker is left to the controller, which
picks up the settings and commands written here on its next pass."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
PROJECT = os.environ["FLEET_PROJECT"]
STREAM, GROUP = "runs", "workers"
# The settings the page may change, with the range each one is kept within.
RANGES = {"desired_workers": (0, 10), "max_active_runs": (1, 50), "max_fleet_spend_usd": (0, 100)}


@asynccontextmanager
async def lifespan(app):
    app.state.db = await asyncpg.create_pool(DATABASE_URL)
    app.state.queue = redis.from_url(REDIS_URL, decode_responses=True)
    yield
    await app.state.db.close()
    await app.state.queue.aclose()


app = FastAPI(title="Agent fleet dashboard", lifespan=lifespan)


@app.get("/")
async def page():
    return FileResponse(Path(__file__).with_name("dashboard.html"))


@app.get("/fleet")
async def fleet():
    db = app.state.db
    workers = await db.fetch("""
        SELECT w.id, w.status, w.drain, w.container, w.runs_done,
               coalesce(a.run_id, w.current_run) AS run_id, r.account_id, a.number AS attempt,
               extract(epoch FROM now() - a.started_at) AS busy_seconds,
               extract(epoch FROM a.lease_expires_at - now()) AS lease_seconds
        FROM workers w
        LEFT JOIN attempts a ON a.worker_id = w.id AND a.status = 'running'
        LEFT JOIN runs r ON r.id = coalesce(a.run_id, w.current_run)
        -- A worker that is gone stays on the page only while it still holds a lease, or is paused.
        WHERE w.status <> 'gone' OR a.run_id IS NOT NULL OR w.container = 'paused'
        ORDER BY substring(w.id FROM 8)::int
    """)
    runs = await db.fetch("""
        SELECT r.id, r.account_id, r.status, a.number AS attempt, a.worker_id, a.turns, a.cost_usd,
               extract(epoch FROM coalesce(a.finished_at, now()) - a.started_at) AS seconds,
               extract(epoch FROM r.updated_at) AS updated
        FROM runs r
        LEFT JOIN LATERAL (SELECT * FROM attempts WHERE run_id = r.id ORDER BY number DESC LIMIT 1) a ON true
        ORDER BY r.created_at
    """)
    totals = await db.fetchrow("""
        SELECT (SELECT count(*) FROM attempts) AS attempts,
               (SELECT count(*) FROM attempts WHERE status = 'lost') AS lost_attempts,
               (SELECT coalesce(sum(cost_usd), 0) FROM attempts) AS cost_usd,
               (SELECT count(*) FROM runs WHERE status = 'accepted'
                   AND updated_at > now() - interval '60 seconds') AS accepted_last_minute
    """)
    events = await db.fetch("""
        SELECT to_char(at, 'HH24:MI:SS') AS at, source, type, run_id, detail->>'summary' AS summary
        FROM events WHERE source = 'controller' OR type IN ('failed', 'result_refused')
        ORDER BY id DESC LIMIT 12
    """)
    groups = await app.state.queue.xinfo_groups(STREAM) if await app.state.queue.exists(STREAM) else []
    group = next((g for g in groups if g["name"] == GROUP), {})
    return {
        "project": PROJECT,
        "settings": dict(await db.fetch("SELECT key, value FROM settings")),
        "queue": {"waiting": group.get("lag") or 0, "delivered": group.get("pending") or 0},
        "workers": [dict(row) for row in workers],
        "runs": [dict(row) for row in runs],
        "totals": dict(totals),
        "events": [dict(row) for row in events],
    }


class Value(BaseModel):
    value: float


@app.post("/settings/{key}")
async def change_setting(key: str, body: Value):
    if key not in RANGES:
        raise HTTPException(404, f"Unknown setting: {key}")
    low, high = RANGES[key]
    value = min(max(body.value, low), high)
    await app.state.db.execute("UPDATE settings SET value = $2 WHERE key = $1", key, value)
    return {"key": key, "value": value}


@app.post("/workers/{worker_id}/{action}")
async def command_worker(worker_id: str, action: str):
    if action == "drain":
        changed = await app.state.db.execute("UPDATE workers SET drain = true WHERE id = $1", worker_id)
    elif action in ("kill", "pause", "resume"):
        changed = await app.state.db.execute("UPDATE workers SET command = $2 WHERE id = $1", worker_id, action)
    else:
        raise HTTPException(404, f"Unknown action: {action}")
    if changed == "UPDATE 0":
        raise HTTPException(404, f"Unknown worker: {worker_id}")
    return {"worker": worker_id, "requested": action}
