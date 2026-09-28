"""Demo 1 worker: take runs from the queue and execute the agent in this container."""

import asyncio
import importlib
import json
import os
import shutil
import socket
from pathlib import Path

import asyncpg
import pandas as pd
import redis.asyncio as redis

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
ACCOUNTS_DIR, WORK_DIR, ARTIFACTS_DIR = Path("/data/accounts"), Path("/work"), Path("/artifacts")
STREAM, GROUP = "runs", "workers"
WORKER_ID = socket.gethostname()
LIMITS = {"max_turns": 15, "max_cost_usd": 0.25, "deadline_seconds": 180}

# The agent lives in agent.py. Tests swap in a fake one through AGENT_MODULE.
run_agent = importlib.import_module(os.environ.get("AGENT_MODULE", "agent")).run_agent


async def record(db, run_id, attempt, event):
    """Save one event to the run's history and print it to the worker's log."""
    await db.execute(
        "INSERT INTO events (run_id, attempt, type, detail) VALUES ($1, $2, $3, $4)",
        run_id, attempt, event["type"], json.dumps(event),
    )
    print(f"[{WORKER_ID}] {run_id}  {event['type']:<15}{event['summary']}", flush=True)


def check_report(csv_path, report):
    """Recompute the figures from the CSV and compare them with the agent's report."""
    usage = pd.read_csv(csv_path)
    expected = {
        "row_count": int(len(usage)),
        "total_units": int(usage["units"].sum()),
        "top_product": str(usage.groupby("product")["units"].sum().idxmax()),
        "peak_day": str(usage.groupby("date")["units"].sum().idxmax()),
    }
    reported = {key: report.get(key) for key in expected} if isinstance(report, dict) else {}
    return {"passed": reported == expected, "expected": expected, "reported": reported}


async def execute_run(db, run_id):
    # 2. Claim the run. The WHERE clause lets a queued run be claimed only once.
    account_id = await db.fetchval(
        "UPDATE runs SET status = 'running', updated_at = now() "
        "WHERE id = $1 AND status = 'queued' RETURNING account_id", run_id,
    )
    if account_id is None:
        return print(f"[{WORKER_ID}] {run_id} is not waiting to be claimed, skipping", flush=True)
    attempt = 1
    await db.execute(
        "INSERT INTO attempts (run_id, number, worker_id) VALUES ($1, $2, $3)",
        run_id, attempt, WORKER_ID,
    )
    await record(db, run_id, attempt, {"type": "claimed", "summary": f"{account_id} by {WORKER_ID}"})

    workdir = WORK_DIR / f"{run_id}-attempt-{attempt}"
    saved = ARTIFACTS_DIR / run_id / f"attempt-{attempt}"
    finished, check, error = {}, None, None
    try:
        # 3. Prepare a fresh working directory with the input and the run's task file.
        workdir.mkdir(parents=True)
        shutil.copy(ACCOUNTS_DIR / f"{account_id}.csv", workdir / "usage.csv")
        task = {"run_id": run_id, "account_id": account_id, "inputs": ["usage.csv"],
                "outputs": ["report.json", "summary.md"], "limits": LIMITS}
        (workdir / "task.json").write_text(json.dumps(task, indent=2))

        # 4. Run the agent in that directory. The deadline covers the whole attempt.
        async with asyncio.timeout(LIMITS["deadline_seconds"]):
            async for event in run_agent(workdir):
                # 5. Record each agent step as it happens.
                await record(db, run_id, attempt, event)
                if event["type"] == "agent_finished":
                    finished = event
        if not finished.get("ok"):
            raise RuntimeError(f"agent stopped early: {finished.get('summary', 'no result')}")

        # 6. Save the outputs outside the working directory, which is about to be deleted.
        saved.mkdir(parents=True)
        for name in task["outputs"]:
            shutil.copy(workdir / name, saved / name)

        # 7. Check the saved report against the input. The decision needs evidence.
        check = check_report(workdir / "usage.csv", json.loads((saved / "report.json").read_text()))
    except Exception as problem:
        error = f"{type(problem).__name__}: {problem}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # 8. Record the outcome of the attempt and of the run.
    status = "failed" if error else "finished"
    await db.execute(
        "UPDATE attempts SET status = $3, turns = $4, input_tokens = $5, output_tokens = $6, "
        "cost_usd = $7, error = $8, finished_at = now() WHERE run_id = $1 AND number = $2",
        run_id, attempt, status, finished.get("turns"),
        finished.get("input_tokens"), finished.get("output_tokens"), finished.get("cost_usd"), error,
    )
    await db.execute(
        "UPDATE runs SET status = $2, report_path = $3, check_result = $4, updated_at = now() "
        "WHERE id = $1",
        run_id, status, str(saved) if check else None,
        json.dumps(check) if check else None,
    )
    summary = error or ("check passed, every figure matches the input" if check["passed"]
                        else "check failed, the report does not match the input")
    await record(db, run_id, attempt, {"type": status, "summary": summary})


async def main():
    db = await asyncpg.create_pool(DATABASE_URL)
    queue = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await queue.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError:
        pass  # the group already exists
    print(f"[{WORKER_ID}] ready, waiting for runs", flush=True)
    while True:
        # 1. Receive one run ID from the queue. Wait up to two seconds, then ask again.
        #    The wait stays shorter than the Redis client's five-second socket timeout.
        reply = await queue.xreadgroup(GROUP, WORKER_ID, {STREAM: ">"}, count=1, block=2000)
        if not reply:
            continue
        message_id, fields = reply[0][1][0]
        await execute_run(db, fields["run_id"])
        # 9. Acknowledge the message only after the outcome is recorded.
        await queue.xack(STREAM, GROUP, message_id)


if __name__ == "__main__":
    asyncio.run(main())
