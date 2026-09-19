"""End-to-end check of Demo 1. It walks the live sequence and asserts each claim.

    uv run python tests/run_demo_1.py                  fake agent, no model calls, no spend
    uv run python tests/run_demo_1.py --live --count 10    real agent, measures time and cost

It uses its own Compose project and API port, so it never touches a running demo.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1] / "demos/01_agent_worker"
PORT = "8011"
API = f"http://localhost:{PORT}"

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
parser.add_argument("--live", action="store_true", help="use the real agent and the model key in .env")
parser.add_argument("--count", type=int, default=3, help="runs in the batch step")
parser.add_argument("--keep", action="store_true", help="leave the containers up afterwards")
args = parser.parse_args()

COMPOSE = ["docker", "compose", "-p", "fleet-demo1-test", "-f", "compose.yaml"]
if not args.live:
    COMPOSE += ["-f", "../../tests/compose.fake.yaml"]
ENV = os.environ | {"API_PORT": PORT}
passed = []


def compose(*command, capture=False):
    result = subprocess.run(COMPOSE + list(command), cwd=DEMO, env=ENV, text=True,
                            capture_output=True)
    if result.returncode != 0:
        sys.exit(f"docker compose {' '.join(command)} failed:\n{result.stderr[-2000:]}")
    return result.stdout if capture else None


def api(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def wait_for(description, condition, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            value = condition()
            if value:
                return value
        except OSError:
            pass
        time.sleep(0.5)
    sys.exit(f"FAILED: timed out waiting for {description}")


def check(claim, ok, detail=""):
    if not ok:
        sys.exit(f"FAILED: {claim} {detail}")
    passed.append(claim)
    print(f"  ok  {claim} {detail}")


def sql(query):
    return compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "fleet", "-Atc", query,
                   capture=True).strip()


peak_memory = {"mib": 0.0}


def sample_worker_memory(stop):
    while not stop.is_set():
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("fleet-demo1-test-worker"):
                used = line.split()[1]
                mib = float(used[:-3]) * (1024 if used.endswith("GiB") else 1)
                peak_memory["mib"] = max(peak_memory["mib"], mib)


print(f"Demo 1 end-to-end check ({'live agent' if args.live else 'fake agent'})")
compose("down", "-v", "--remove-orphans")
compose("up", "-d", "--build", "api")
wait_for("the API", lambda: api("/runs") is not None, 90)
services = compose("ps", "--services", "--status", "running", capture=True).split()
check("API, Postgres, and Redis are up and the worker is stopped", sorted(services) == ["api", "postgres", "redis"])

# Submit one run with no worker.
started = time.perf_counter()
run = api("/runs", {"account_id": "acct-001"})
elapsed_ms = (time.perf_counter() - started) * 1000
check("submission returns a run ID in under 200 ms", elapsed_ms < 200, f"({elapsed_ms:.0f} ms)")
time.sleep(3)
check("the run waits queued while no worker exists", api(run["status_url"])["status"] == "queued")
try:
    api("/runs", {"account_id": "acct-999"})
    check("an unknown account is refused", False)
except urllib.error.HTTPError as error:
    check("an unknown account is refused", error.code == 404)

# Start the worker, then restart the API while the run is in progress.
stop = threading.Event()
threading.Thread(target=sample_worker_memory, args=(stop,), daemon=True).start()
compose("up", "-d", "worker")
wait_for("the worker to claim the run", lambda: api(run["status_url"])["status"] == "running", 60)
if args.live:
    top = wait_for("the agent process", lambda: "claude" in compose("top", "worker", capture=True) and
                   compose("top", "worker", capture=True), 60)
    check("the agent runs as a child process inside the worker's container", "claude" in top)
compose("restart", "api")
wait_for("the API after its restart", lambda: api("/runs") is not None, 60)
final = wait_for("the run to finish", lambda: (r := api(run["status_url"]))["status"] in ("accepted", "failed") and r, 240)
check("a run in progress survives an API restart and is accepted", final["status"] == "accepted",
      json.dumps(final.get("check_result") or final["attempts"][-1].get("error"))[:200])
check("the report is served from the artifacts volume", final["report"] == final["check_result"]["expected"])
attempt = final["attempts"][0]
check("usage and cost are recorded on the attempt",
      attempt["turns"] is not None and attempt["cost_usd"] is not None and attempt["status"] == "finished")
kinds = sql(f"SELECT string_agg(DISTINCT type, ',') FROM events WHERE run_id = '{run['run_id']}'").split(",")
check("events cover submission, claim, agent steps, and the decision",
      {"submitted", "claimed", "tool_call", "agent_finished", "accepted"} <= set(kinds))

# Submit a batch for the single worker.
batch = [api("/runs", {"account_id": f"acct-{n:03d}"}) for n in range(2, args.count + 2)]
check(f"{args.count} more submissions all return at once", len(batch) == args.count)
wait_for("the batch to finish",
         lambda: all(r["status"] in ("accepted", "failed") for r in api("/runs")), 240 * args.count)
stop.set()
runs = api("/runs")
check("every run is accepted", all(r["status"] == "accepted" for r in runs),
      str([(r["id"], r["status"]) for r in runs if r["status"] != "accepted"]))
check("one worker executed every run", len({r["worker_id"] for r in runs}) == 1)
overlaps = sql("SELECT count(*) FROM attempts a JOIN attempts b ON a.run_id < b.run_id "
               "AND a.started_at < b.finished_at AND b.started_at < a.finished_at")
check("the worker ran them one at a time", overlaps == "0")
leftovers = compose("exec", "-T", "worker", "sh", "-c", "ls -A /work | wc -l", capture=True).strip()
check("no working directories remain", leftovers == "0")
saved = compose("exec", "-T", "worker", "sh", "-c", "ls /artifacts/*/attempt-1/report.json | wc -l", capture=True).strip()
check("every accepted run has a saved report", int(saved) == len(runs))

# An idle worker must keep waiting on the queue and still take the next run.
time.sleep(12)
services = compose("ps", "--services", "--status", "running", capture=True).split()
check("the worker is still running after sitting idle", "worker" in services)
late = api("/runs", {"account_id": "acct-040"})
wait_for("the late run", lambda: api(late["status_url"])["status"] in ("accepted", "failed"), 240)
check("the idle worker takes a run submitted later", api(late["status_url"])["status"] == "accepted")
runs = api("/runs")

seconds = float(sql("SELECT avg(extract(epoch FROM finished_at - started_at)) FROM attempts"))
cost = float(sql("SELECT coalesce(sum(cost_usd), 0) FROM attempts"))
turns = float(sql("SELECT avg(turns) FROM attempts"))
print(f"\nMeasured over {len(runs)} runs: {seconds:.1f} s per run, {turns:.1f} turns per run, "
      f"${cost:.3f} total, ${cost / len(runs):.4f} per run, worker memory peak {peak_memory['mib']:.0f} MiB")
print(f"That is about {3600 / seconds:.0f} runs an hour for one worker.")
if not args.keep:
    compose("down", "-v", "--remove-orphans")
print(f"\nAll {len(passed)} checks passed.")
