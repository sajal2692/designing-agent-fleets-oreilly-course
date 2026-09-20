"""Demo 2 controller: the fleet's control loop. Every two seconds it compares what exists
with what is wanted, and acts. It is the only service that can start or stop a worker."""

import asyncio
import json
import os
import signal

import asyncpg
import redis.asyncio as redis

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
PROJECT = os.environ["FLEET_PROJECT"]                # names this fleet's containers and network
IMAGE = os.environ.get("WORKER_IMAGE", "agent-fleet-demo2")
STREAM, GROUP = "runs", "workers"
MAX_WORKERS, MAX_ATTEMPTS, SILENT_SECONDS = 10, 2, 15
# Passed from this container's environment into every worker it starts.
WORKER_ENV = ["DATABASE_URL", "REDIS_URL", "ANTHROPIC_API_KEY", "AGENT_MODULE",
              "FAKE_AGENT_SECONDS", "MAX_RUNS_PER_WORKER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]


async def docker(*args):
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, _ = await process.communicate()
    return output.decode().strip()


async def note(db, event_type, summary, run_id=None, attempt=None):
    """Record a decision. The dashboard's event list reads these."""
    await db.execute(
        "INSERT INTO events (run_id, attempt, source, type, detail) VALUES ($1, $2, 'controller', $3, $4)",
        run_id, attempt, event_type, json.dumps({"type": event_type, "summary": summary}),
    )
    print(f"[controller] {event_type:<16}{summary}", flush=True)


def plan_workers(wanted, workers, starting):
    """Decide how many workers to start and which to drain. Idle workers drain first."""
    serving = [worker for worker in workers if not worker["drain"]]
    missing = wanted - len(serving) - starting
    idle_first = sorted(serving, key=lambda worker: worker["status"] == "busy")
    return max(missing, 0), [worker["id"] for worker in idle_first[:max(-missing, 0)]]


async def start_worker(db):
    number = int(await db.fetchval(
        "UPDATE settings SET value = value + 1 WHERE key = 'next_worker' RETURNING value")) - 1
    env = [flag for name in WORKER_ENV if name in os.environ for flag in ("-e", name)]
    await docker("run", "--detach", "--name", f"{PROJECT}-worker-{number}", "--hostname", f"worker-{number}",
                 "--network", f"{PROJECT}_net", "--volume", f"{PROJECT}_artifacts:/artifacts",
                 "--label", f"fleet.project={PROJECT}", "--label", f"fleet.worker=worker-{number}",
                 "--memory", "1g", "--cpus", "1", *env, IMAGE, "python", "-u", "worker.py")
    return f"worker-{number}"


async def requeue(db, queue, run_id, message_id):
    await queue.xack(STREAM, GROUP, message_id)
    await queue.xadd(STREAM, {"run_id": run_id})


async def reconcile(db, queue):
    # 1. Observe: what Docker is running, what the workers report, and what is wanted.
    listing = await docker("ps", "--all", "--filter", f"label=fleet.project={PROJECT}",
                           "--format", '{{.Label "fleet.worker"}} {{.State}}')
    containers = dict(line.split() for line in listing.splitlines())
    settings = dict(await db.fetch("SELECT key, value FROM settings"))
    known = {row["id"] for row in await db.fetch("SELECT id FROM workers")}
    for worker_id, state in containers.items():
        await db.execute("UPDATE workers SET container = $2 WHERE id = $1 AND container IS DISTINCT FROM $2",
                         worker_id, state)

    # 2. Carry out what the operator asked for on the dashboard: kill, pause, or resume a worker.
    for worker in await db.fetch("SELECT id, command FROM workers WHERE command IS NOT NULL"):
        verb = {"kill": "kill", "pause": "pause", "resume": "unpause"}[worker["command"]]
        await docker(verb, f"{PROJECT}-{worker['id']}")
        await db.execute("UPDATE workers SET command = NULL WHERE id = $1", worker["id"])
        await note(db, f"worker_{worker['command']}", f"{worker['id']}: {worker['command']} requested on the dashboard")

    # 3. Workers that are gone: the container is not running, or nothing heard for too long.
    #    Losing contact does not prove the worker stopped, so its lease is left to run out.
    silent = await db.fetch(
        "SELECT id, container FROM workers WHERE status <> 'gone' AND "
        "(container <> 'running' OR heartbeat_at < now() - $1 * interval '1 second')",
        SILENT_SECONDS)
    for worker in silent:
        await db.execute("UPDATE workers SET status = 'gone', drain = true WHERE id = $1", worker["id"])
        await note(db, "worker_gone", f"{worker['id']}: no contact, container is {worker['container'] or 'missing'}")
        # A run this worker was only holding, not executing, goes back for another worker.
        for held in await queue.xpending_range(STREAM, GROUP, "-", "+", 10, consumername=worker["id"]):
            run_id = (await queue.xrange(STREAM, held["message_id"], held["message_id"]))[0][1]["run_id"]
            if await db.fetchval("SELECT status FROM runs WHERE id = $1", run_id) == "queued":
                await requeue(db, queue, run_id, held["message_id"])
                await note(db, "run_requeued", f"held by {worker['id']}, queued again", run_id)

    # 4. Expired leases: the attempt is lost. Try the run again, up to the attempt limit.
    expired = await db.fetch(
        "UPDATE attempts SET status = 'lost', error = 'lease expired', finished_at = now() "
        "WHERE status = 'running' AND lease_expires_at < now() RETURNING run_id, number, worker_id, message_id")
    for attempt in expired:
        again = attempt["number"] < MAX_ATTEMPTS
        await db.execute("UPDATE runs SET status = $2, updated_at = now() WHERE id = $1",
                         attempt["run_id"], "queued" if again else "failed")
        if again:
            await requeue(db, queue, attempt["run_id"], attempt["message_id"])
        else:
            await queue.xack(STREAM, GROUP, attempt["message_id"])
        outcome = f"queued again as attempt {attempt['number'] + 1}" if again else "failed, attempt limit reached"
        await note(db, "attempt_lost", f"lease expired on {attempt['worker_id']}, {outcome}",
                   attempt["run_id"], attempt["number"])

    # 5. Worker count: start what is missing, drain what is surplus. A container that is
    #    running but has not registered yet counts as starting, so it is not started twice.
    wanted = min(int(settings["desired_workers"]), MAX_WORKERS)
    workers = await db.fetch("SELECT id, status, drain FROM workers WHERE status <> 'gone'")
    starting = sum(1 for worker_id, state in containers.items() if state == "running" and worker_id not in known)
    to_start, to_drain = plan_workers(wanted, workers, starting)
    for _ in range(to_start):
        await note(db, "worker_started", f"{await start_worker(db)} started, {wanted} wanted")
    for worker_id in to_drain:
        await db.execute("UPDATE workers SET drain = true WHERE id = $1", worker_id)
        await note(db, "worker_draining", f"{worker_id} will finish its run and leave, {wanted} wanted")

    # 6. Clean up containers of workers that have exited.
    for worker_id, state in containers.items():
        if state == "exited":
            await docker("rm", f"{PROJECT}-{worker_id}")
            await db.execute("UPDATE workers SET status = 'gone', container = 'removed' WHERE id = $1", worker_id)


async def main():
    db = await asyncpg.create_pool(DATABASE_URL)
    queue = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await queue.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError:
        pass  # the group already exists
    stopping = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
    print("[controller] ready", flush=True)
    while not stopping.is_set():
        try:
            await reconcile(db, queue)
        except Exception as problem:  # one bad pass must not end the loop
            print(f"[controller] pass failed: {type(problem).__name__}: {problem}", flush=True)
        await asyncio.sleep(2)

    # Shutting down: this fleet's workers go with it, so `docker compose down` leaves nothing behind.
    names = await docker("ps", "--all", "--quiet", "--filter", f"label=fleet.project={PROJECT}")
    if names:
        await docker("rm", "--force", *names.split())


if __name__ == "__main__":
    asyncio.run(main())
