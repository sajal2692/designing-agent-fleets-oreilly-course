"""Demo 1 API: accept a run, queue it, and serve its status. It never runs an agent."""

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
ACCOUNTS_DIR = Path("/data/accounts")
STREAM, GROUP = "runs", "workers"


@asynccontextmanager
async def lifespan(app):
    app.state.db = await asyncpg.create_pool(DATABASE_URL)
    app.state.queue = redis.from_url(REDIS_URL, decode_responses=True)
    # Create the queue and its consumer group up front, so runs submitted before any
    # worker exists are still waiting when the first worker starts.
    try:
        await app.state.queue.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError:
        pass  # the group already exists
    yield
    await app.state.db.close()
    await app.state.queue.aclose()


app = FastAPI(title="Agent fleet API", lifespan=lifespan)


class RunRequest(BaseModel):
    account_id: str


@app.post("/runs", status_code=202)
async def submit_run(request: RunRequest):
    if not (ACCOUNTS_DIR / f"{request.account_id}.csv").exists():
        raise HTTPException(404, f"Unknown account: {request.account_id}")
    run_id = f"run-{uuid.uuid4().hex[:8]}"

    # 1. Record the task durably before anything else.
    async with app.state.db.acquire() as db, db.transaction():
        await db.execute(
            "INSERT INTO runs (id, account_id) VALUES ($1, $2)", run_id, request.account_id
        )
        await db.execute("INSERT INTO events (run_id, type) VALUES ($1, 'submitted')", run_id)

    # 2. Queue the run for a worker. The message carries only the run ID.
    await app.state.queue.xadd(STREAM, {"run_id": run_id})

    # 3. Return at once. The client never waits for the agent.
    return {"run_id": run_id, "status": "queued", "status_url": f"/runs/{run_id}"}


@app.get("/runs/{run_id}")
async def read_run(run_id: str):
    run = await app.state.db.fetchrow("SELECT * FROM runs WHERE id = $1", run_id)
    if run is None:
        raise HTTPException(404, f"Unknown run: {run_id}")
    attempts = await app.state.db.fetch(
        "SELECT * FROM attempts WHERE run_id = $1 ORDER BY number", run_id
    )
    result = dict(run) | {"attempts": [dict(attempt) for attempt in attempts]}
    if run["check_result"]:
        result["check_result"] = json.loads(run["check_result"])
    # Run records live in Postgres. The report itself lives on the artifacts volume.
    if run["report_path"]:
        result["report"] = json.loads(Path(run["report_path"], "report.json").read_text())
    return result


@app.get("/runs")
async def list_runs():
    rows = await app.state.db.fetch("""
        SELECT r.id, r.account_id, r.status, (r.check_result->>'passed')::boolean AS check_passed,
               r.created_at, a.worker_id, a.turns, a.cost_usd, a.started_at, a.finished_at
        FROM runs r LEFT JOIN attempts a ON a.run_id = r.id
        ORDER BY r.created_at
    """)
    return [dict(row) for row in rows]
