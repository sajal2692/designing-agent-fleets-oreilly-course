"""Demo 2 worker: one member of a pool. It takes runs from the queue, executes the agent in
this container, and keeps a lease on the attempt so the fleet can tell if it disappears."""

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
HEARTBEAT_SECONDS, LEASE_SECONDS = 5, 20
MAX_RUNS = int(os.environ.get("MAX_RUNS_PER_WORKER", "0"))  # 0 means no recycling

# The agent lives in agent.py. Tests swap in a fake one through AGENT_MODULE.
run_agent = importlib.import_module(os.environ.get("AGENT_MODULE", "agent")).run_agent

# What the heartbeat task and the main loop share: the attempt in hand, and the drain flag.
state = {"attempt": None, "drain": False}


async def record(db, run_id, attempt, event):
    """Save one event to the run's history and print it to the worker's log."""
    await db.execute(
        "INSERT INTO events (run_id, attempt, source, type, detail) VALUES ($1, $2, $3, $4, $5)",
        run_id, attempt, WORKER_ID, event["type"], json.dumps(event),
    )
    print(f"[{WORKER_ID}] {run_id}  {event['type']:<15}{event['summary']}", flush=True)


async def report(db, status, run_id=None):
    await db.execute(
        "UPDATE workers SET status = $2, current_run = $3, heartbeat_at = now() WHERE id = $1",
        WORKER_ID, status, run_id,
    )


async def heartbeat(db):
    """Every five seconds: tell the fleet this worker is alive, and renew the lease on the
    attempt in hand. If the worker stops, the lease runs out and the controller notices."""
    while True:
        drain = await db.fetchval(
            "UPDATE workers SET heartbeat_at = now() WHERE id = $1 RETURNING drain", WORKER_ID)
        state["drain"] = drain is not False  # a missing row also means: finish up and leave
        if state["attempt"]:
            await db.execute(
                "UPDATE attempts SET lease_expires_at = now() + $3 * interval '1 second' "
                "WHERE run_id = $1 AND number = $2 AND status = 'running'",
                *state["attempt"], LEASE_SECONDS,
            )
        await asyncio.sleep(HEARTBEAT_SECONDS)


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


async def claim(db, run_id, message_id):
    """Claim a queued run if the fleet's limits allow it. Returns the account and attempt
    number, "wait" when a limit is reached, or None when the run is not there to claim."""
    async with db.acquire() as conn, conn.transaction():
        # One claim at a time across the fleet, so two workers cannot take the last place.
        await conn.execute("SELECT pg_advisory_xact_lock(1)")
        limits = dict(await conn.fetch("SELECT key, value FROM settings"))
        active = await conn.fetchval("SELECT count(*) FROM runs WHERE status = 'running'")
        spent = await conn.fetchval("SELECT coalesce(sum(cost_usd), 0) FROM attempts")
        if active >= limits["max_active_runs"] or spent >= limits["max_fleet_spend_usd"]:
            return "wait"
        account_id = await conn.fetchval(
            "UPDATE runs SET status = 'running', updated_at = now() "
            "WHERE id = $1 AND status = 'queued' RETURNING account_id", run_id,
        )
        if account_id is None:
            return None
        number = await conn.fetchval(
            "INSERT INTO attempts (run_id, number, worker_id, message_id, lease_expires_at) "
            "SELECT $1, coalesce(max(number), 0) + 1, $2, $3, now() + $4 * interval '1 second' "
            "FROM attempts WHERE run_id = $1 RETURNING number",
            run_id, WORKER_ID, message_id, LEASE_SECONDS,
        )
        return account_id, number


async def execute_run(db, run_id, account_id, attempt):
    await record(db, run_id, attempt, {"type": "claimed", "summary": f"{account_id}, attempt {attempt}"})
    workdir = WORK_DIR / f"{run_id}-attempt-{attempt}"
    saved = ARTIFACTS_DIR / run_id / f"attempt-{attempt}"
    finished, check, error = {}, None, None
    try:
        # 3. Prepare a fresh working directory, and refuse an input the task cannot use.
        workdir.mkdir(parents=True)
        shutil.copy(ACCOUNTS_DIR / f"{account_id}.csv", workdir / "usage.csv")
        if (workdir / "usage.csv").read_text().splitlines()[0] != "date,product,units":
            raise ValueError("usage.csv does not have the columns this task requires")
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

    # 8. Record the attempt's outcome, but only if this attempt still owns the run. If the
    #    lease ran out, the controller marked it lost and another attempt may have finished.
    still_ours = await db.execute(
        "UPDATE attempts SET status = $3, turns = $4, input_tokens = $5, output_tokens = $6, "
        "cost_usd = $7, error = $8, finished_at = now() "
        "WHERE run_id = $1 AND number = $2 AND status = 'running'",
        run_id, attempt, "failed" if error else "finished", finished.get("turns"),
        finished.get("input_tokens"), finished.get("output_tokens"), finished.get("cost_usd"), error,
    )
    if still_ours == "UPDATE 0":
        return await record(db, run_id, attempt, {
            "type": "result_refused", "summary": "this attempt lost its lease, so its result is discarded"})

    accepted = error is None and check["passed"]
    await db.execute(
        "UPDATE runs SET status = $2, report_path = $3, check_result = $4, updated_at = now() "
        "WHERE id = $1",
        run_id, "accepted" if accepted else "failed", str(saved) if check else None,
        json.dumps(check) if check else None,
    )
    summary = "every figure matches the input" if accepted else error or "the report does not match the input"
    await record(db, run_id, attempt, {"type": "accepted" if accepted else "failed", "summary": summary})


async def main():
    db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)  # many workers share one database
    queue = redis.from_url(REDIS_URL, decode_responses=True)
    await db.execute(
        "INSERT INTO workers (id) VALUES ($1) ON CONFLICT (id) DO UPDATE "
        "SET status = 'ready', drain = false, started_at = now(), heartbeat_at = now()", WORKER_ID)
    beat = asyncio.create_task(heartbeat(db))
    print(f"[{WORKER_ID}] ready, waiting for runs", flush=True)

    runs_done = 0
    while not state["drain"] and not (MAX_RUNS and runs_done >= MAX_RUNS):
        # 1. Receive one run ID from the queue. Wait up to two seconds, then ask again.
        #    The wait stays shorter than the Redis client's five-second socket timeout.
        reply = await queue.xreadgroup(GROUP, WORKER_ID, {STREAM: ">"}, count=1, block=2000)
        if not reply:
            continue
        message_id, fields = reply[0][1][0]
        run_id = fields["run_id"]

        # 2. Claim the run. While a fleet limit is reached, hold the message and ask again.
        while (claimed := await claim(db, run_id, message_id)) == "wait" and not state["drain"]:
            await report(db, "waiting", run_id)
            await asyncio.sleep(1)
        if claimed == "wait":  # told to leave while waiting: put the run back for another worker
            await queue.xadd(STREAM, {"run_id": run_id})
        elif claimed:
            account_id, attempt = claimed
            state["attempt"] = (run_id, attempt)
            await report(db, "busy", run_id)
            await execute_run(db, run_id, account_id, attempt)
            state["attempt"] = None
            runs_done += 1
            await db.execute("UPDATE workers SET runs_done = $2 WHERE id = $1", WORKER_ID, runs_done)
        # 9. Acknowledge the message only after the outcome is recorded.
        await queue.xack(STREAM, GROUP, message_id)
        await report(db, "ready")

    beat.cancel()
    await report(db, "gone")
    print(f"[{WORKER_ID}] leaving after {runs_done} runs", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
