"""End-to-end check of Demo 2. It drives the fleet the way the live demo does and asserts
each claim.

    uv run python tests/run_demo_2.py            fake agent, no model calls, no spend
    uv run python tests/run_demo_2.py --live     real agent, about 95 runs, measures throughput and cost
    uv run python tests/run_demo_2.py --live --quick    real agent, about 50 runs, failure steps only

It uses its own fleet name and ports, so it never touches a running demo.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demos/02_agent_fleet"
PROJECT = "agent-fleet-test"
API, DASHBOARD = "http://localhost:8012", "http://localhost:8082"

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
parser.add_argument("--live", action="store_true", help="use the real agent and the model key in .env")
parser.add_argument("--count", type=int, help="runs in the batch (default 100 fake, 70 live)")
parser.add_argument("--quick", action="store_true", help="skip the throughput stages: failure and limit steps only")
parser.add_argument("--keep", action="store_true", help="leave the fleet up afterwards")
args = parser.parse_args()
COUNT = args.count or (24 if args.quick else 70 if args.live else 100)
STAGE = 60 if args.live else 30   # seconds to watch throughput at each worker count

ENV = os.environ | {"FLEET_PROJECT": PROJECT, "API_PORT": "8012", "DASHBOARD_PORT": "8082"}
if not args.live:
    ENV |= {"COMPOSE_FILE": "compose.yaml:../../tests/compose.fleet.fake.yaml",
            "WORKER_IMAGE": "agent-fleet-demo2-fake", "FAKE_AGENT_SECONDS": "6"}
passed = []


def run(*command, cwd=DEMO, check_exit=True):
    result = subprocess.run(command, cwd=cwd, env=ENV, text=True, capture_output=True)
    if check_exit and result.returncode != 0:
        sys.exit(f"{' '.join(command)} failed:\n{result.stderr[-2000:]}")
    return result.stdout


def call(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def fleet():
    return call(f"{DASHBOARD}/fleet")


def setting(key, value):
    call(f"{DASHBOARD}/settings/{key}", {"value": value})


def wait_for(description, condition, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            value = condition()
            if value:
                return value
        except (OSError, KeyError, IndexError):
            pass
        time.sleep(0.5)
    sys.exit(f"FAILED: timed out waiting for {description}")


def check(claim, ok, detail=""):
    if not ok:
        sys.exit(f"FAILED: {claim} {detail}")
    passed.append(claim)
    print(f"  ok  {claim} {detail}")


def sql(query):
    return run("docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "fleet",
               "-Atc", query).strip()


def serving():
    return [w for w in fleet()["workers"] if w["status"] != "gone" and not w["drain"]]


def busy_worker(exclude=()):
    return next((w for w in fleet()["workers"]
                 if w["status"] == "busy" and w["attempt"] and w["id"] not in exclude), None)


def more_runs(count):
    """Keep work in progress, so the failure steps always have a busy worker to act on."""
    for n in range(count):
        call(f"{API}/runs", {"account_id": f"acct-{n + 1:03d}"})


def accepted():
    return sum(run_["status"] == "accepted" for run_ in fleet()["runs"])


peak = {"running": 0, "memory_mib": 0.0}


def sample(stop):
    """Track the most runs ever in progress at once, and the workers' combined memory."""
    while not stop.is_set():
        try:
            peak["running"] = max(peak["running"], sum(r["status"] == "running" for r in fleet()["runs"]))
        except OSError:
            pass
        if args.live:
            out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],
                                 capture_output=True, text=True).stdout
            used = [line.split()[1] for line in out.splitlines() if line.startswith(f"{PROJECT}-worker")]
            total = sum(float(u[:-3]) * (1024 if u.endswith("GiB") else 1) for u in used)
            peak["memory_mib"] = max(peak["memory_mib"], total)
        time.sleep(0.5)


print(f"Demo 2 end-to-end check ({'live agent' if args.live else 'fake agent'}, {COUNT} runs)")
run("docker", "compose", "down", "-v", "--remove-orphans", check_exit=False)
run("docker", "compose", "build")
if not args.live:
    run("docker", "build", "-q", "-t", "agent-fleet-demo2-fake", "-f", "Dockerfile.fake", ".", cwd=ROOT / "tests")
run("docker", "compose", "up", "-d")
wait_for("the dashboard", lambda: fleet()["settings"], 120)

# The controller brings the fleet to its first target without being told twice.
wait_for("two workers", lambda: len(serving()) == 2, 60)
check("the controller starts two workers from a target of two", len(serving()) == 2)

TARGET = 4 if args.quick else 8
if args.quick:
    for n in range(COUNT):
        call(f"{API}/runs", {"account_id": f"acct-{n % 40 + 1:03d}"})
    stop = threading.Event()
    threading.Thread(target=sample, args=(stop,), daemon=True).start()
    setting("desired_workers", TARGET)
    wait_for("four workers", lambda: len(serving()) == TARGET, 90)
    check("raising the target to four starts two more workers", True)
    rate_2 = rate_limited = rate_4 = rate_8 = 0
else:
    # A fleet limit below the worker count caps runs in progress, however many workers exist.
    setting("max_active_runs", 2)
    started = time.perf_counter()
    for n in range(COUNT):
        call(f"{API}/runs", {"account_id": f"acct-{n % 40 + 1:03d}"})
    check(f"{COUNT} submissions return promptly", time.perf_counter() - started < 10,
          f"({time.perf_counter() - started:.1f} s)")
    stop = threading.Event()
    threading.Thread(target=sample, args=(stop,), daemon=True).start()
    before = accepted(); time.sleep(STAGE)
    rate_2 = (accepted() - before) * 60 / STAGE

    setting("desired_workers", 4)
    wait_for("four workers", lambda: len(serving()) == 4, 60)
    check("raising the target to four starts two more workers", len(serving()) == 4)
    wait_for("workers held at the fleet limit", lambda: any(w["status"] == "waiting" for w in fleet()["workers"]), 40)
    before = accepted(); time.sleep(STAGE)
    rate_limited = (accepted() - before) * 60 / STAGE
    check("runs in progress never exceed the fleet limit of two", peak["running"] <= 2, f"(peak {peak['running']})")
    check("four workers at a limit of two are no faster than two workers",
          rate_limited <= rate_2 * 1.35, f"({rate_2:.0f} then {rate_limited:.0f} runs a minute)")

    setting("max_active_runs", 8)
    before = accepted(); time.sleep(STAGE)
    rate_4 = (accepted() - before) * 60 / STAGE
    check("raising the limit lets four workers outrun two", rate_4 > rate_2 * 1.4,
          f"({rate_2:.0f} then {rate_4:.0f} runs a minute)")

    # The scale the live demo reaches: eight workers.
    setting("desired_workers", 8)
    wait_for("eight workers", lambda: len(serving()) == 8, 90)
    before = accepted(); time.sleep(STAGE)
    rate_8 = (accepted() - before) * 60 / STAGE
    check("eight workers outrun four", rate_8 > rate_4 * 1.3, f"({rate_4:.0f} then {rate_8:.0f} runs a minute)")

    # Kill a busy worker. Its lease runs out, the run goes to another worker, capacity is restored.
more_runs(12)
victim = wait_for("a busy worker", busy_worker, 60)
call(f"{DASHBOARD}/workers/{victim['id']}/kill", {})
def lost_attempt(worker_id):
    """The kill or pause lands on the controller's next pass, by which time the worker may
    have moved to another run. Follow the attempt it actually lost."""
    found = wait_for(f"an attempt lost by {worker_id}", lambda: sql(
        f"SELECT run_id || ' ' || number FROM attempts WHERE worker_id = '{worker_id}' AND status = 'lost'"), 120)
    run_id, number = found.split()
    return run_id, int(number)


lost_run, lost_number = lost_attempt(victim["id"])
check("a killed worker's attempt is marked lost when its lease expires", True, f"({victim['id']}, {lost_run})")
wait_for("the recovered run", lambda: sql(f"SELECT status FROM runs WHERE id = '{lost_run}'") == "accepted", 240)
second = sql(f"SELECT worker_id FROM attempts WHERE run_id = '{lost_run}' AND number = {lost_number + 1}")
check("the run is accepted on a second attempt by another worker", second not in ("", victim["id"]), f"({second})")
wait_for("capacity restored", lambda: len(serving()) == TARGET, 60)
check("the controller replaces the lost worker", len(serving()) == TARGET)

# Pause a busy worker, let its run be recovered, then resume it. Its late result is refused.
more_runs(12)
frozen = wait_for("another busy worker", lambda: busy_worker(exclude={victim["id"]}), 60)
call(f"{DASHBOARD}/workers/{frozen['id']}/pause", {})
frozen_run, frozen_number = lost_attempt(frozen["id"])
wait_for("the paused worker's run to be accepted elsewhere",
         lambda: sql(f"SELECT status FROM runs WHERE id = '{frozen_run}'") == "accepted", 240)
winner = sql(f"SELECT report_path FROM runs WHERE id = '{frozen_run}'")
call(f"{DASHBOARD}/workers/{frozen['id']}/resume", {})
wait_for("the refusal", lambda: sql(
    f"SELECT count(*) FROM events WHERE run_id = '{frozen_run}' AND type = 'result_refused'") == "1", 240)
check("a paused worker's late result is refused", True, f"({frozen['id']}, {frozen_run})")
check("the accepted result still stands", sql(f"SELECT report_path FROM runs WHERE id = '{frozen_run}'") == winner
      and winner.endswith(f"attempt-{frozen_number + 1}"))

# An input the task cannot use fails once and is not retried.
bad = call(f"{API}/runs", {"account_id": "acct-corrupt"})
wait_for("the corrupt account to fail", lambda: sql(f"SELECT status FROM runs WHERE id = '{bad['run_id']}'") == "failed", 120)
check("a corrupt input fails after one attempt with the reason recorded",
      sql(f"SELECT count(*) || ' ' || max(error) FROM attempts WHERE run_id = '{bad['run_id']}'").startswith("1 ValueError"))

# Let the backlog finish, then test the spend limit on an idle fleet.
wait_for("the backlog to finish", lambda: all(r["status"] in ("accepted", "failed") for r in fleet()["runs"]), 900)
stop.set()
runs = fleet()["runs"]
check("every run except the corrupt one is accepted",
      [r["id"] for r in runs if r["status"] != "accepted"] == [bad["run_id"]])
setting("max_fleet_spend_usd", 0)
held = call(f"{API}/runs", {"account_id": "acct-001"})
time.sleep(8)
check("no run is claimed once the spend limit is reached",
      sql(f"SELECT status FROM runs WHERE id = '{held['run_id']}'") == "queued")
setting("max_fleet_spend_usd", 5)
wait_for("the held run", lambda: sql(f"SELECT status FROM runs WHERE id = '{held['run_id']}'") == "accepted", 120)
check("raising the spend limit releases the held run", True)

# Scale down. Workers drain and their containers are removed.
setting("desired_workers", 2)
wait_for("two workers", lambda: len(serving()) == 2 and len(run(
    "docker", "ps", "-q", "--filter", f"label=fleet.project={PROJECT}").split()) == 2, 90)
check("lowering the target to two drains the rest and removes their containers", True)

attempts, lost = sql("SELECT count(*) FROM attempts"), sql("SELECT count(*) FROM attempts WHERE status = 'lost'")
cost = float(sql("SELECT coalesce(sum(cost_usd), 0) FROM attempts"))
done = sum(r["status"] == "accepted" for r in fleet()["runs"])
print(f"\n{done} runs accepted from {attempts} attempts, {lost} lost.")
if not args.quick:
    print(f"Runs a minute: {rate_2:.0f} at two workers, {rate_limited:.0f} at four workers held to two, "
          f"{rate_4:.0f} at four workers, {rate_8:.0f} at eight.")
if args.live:
    print(f"Cost ${cost:.2f}, ${cost / done:.4f} per accepted run. Workers' combined memory peaked at "
          f"{peak['memory_mib']:.0f} MiB.")

if not args.keep:
    run("docker", "compose", "down", "-v", "--remove-orphans")
    left = run("docker", "ps", "-aq", "--filter", f"label=fleet.project={PROJECT}").split()
    check("nothing is left behind after docker compose down", left == [])
print(f"\nAll {len(passed)} checks passed.")
