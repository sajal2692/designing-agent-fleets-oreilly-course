# Demo 1: Agent Execution Inside a Worker

An API accepts a task and returns a run ID at once. A separate worker takes the run
from a queue, executes an agent inside its own container, checks the result, and records
the outcome. Then ten runs go to the same worker, and you watch the queue drain one run
at a time.

The task is small on purpose. Each run gets one customer's `usage.csv`, 90 rows of daily
usage for three products. The agent writes `report.json` with four figures and a short
`summary.md`. The worker recomputes the four figures from the same CSV and records whether
they match.

```text
submit.py ──> api ──> postgres   run records: runs, attempts, events
               │
               └────> redis      queue: carries only the run ID
                        │
                     worker ──> agent (Claude Agent SDK, a child process in the worker's container)
                        │
                        └─────> artifacts volume   report.json and summary.md per run
```

## Setup

You need Docker Desktop, Python 3.12 or newer, and an Anthropic API key. A full
walkthrough of twelve runs cost about 22 cents with Claude Haiku 4.5 in September 2026.

From the repository root, create `.env` and add your key:

```bash
cp .env.example .env
```

Then build the image and start everything except the worker:

```bash
cd demos/01_agent_worker
docker compose up -d --build api
```

`submit.py` and `watch.py` use only the Python standard library, so they run on your
machine with no installation.

## The walkthrough

Open two terminals in `demos/01_agent_worker`. Keep this running in the second one:

```bash
python3 watch.py
```

**1. Look at what is running.** The API, Postgres, and Redis are up. There is no worker.

```bash
docker compose ps
```

**2. Submit one run.**

```bash
python3 submit.py acct-001
```

```text
run-25690114  acct-001  queued  returned in 6 ms
```

The API saved the run, put its ID on the queue, and answered. The watch pane shows it
queued. Nothing can execute it yet.

**3. Start the worker and follow it.**

```bash
docker compose up -d worker
docker compose logs -f worker
```

```text
[worker-1] ready, waiting for runs
[worker-1] run-25690114  claimed        acct-001 by worker-1
[worker-1] run-25690114  tool_call      Read: /work/run-25690114-attempt-1/usage.csv
[worker-1] run-25690114  tool_call      Bash: python3 << 'EOF'
[worker-1] run-25690114  tool_call      Write: /work/run-25690114-attempt-1/summary.md
[worker-1] run-25690114  agent_finished success, 8 turns, 58,897 tokens in, $0.026
[worker-1] run-25690114  finished       check passed, every figure matches the input
```

While a run is in progress, list the processes inside the worker's container:

```bash
docker compose top worker
```

```text
UID   PID    PPID   CMD
1000  22874  22847  python -u worker.py
1000  23497  22874  /usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude --output-format stream-json ...
```

The second process is the agent. The SDK started it as a child of the worker, in the
same container, and the two talk over stdin and stdout.

**4. Restart the API in the middle of a run.** Submit another run, wait for it to show
`running`, then:

```bash
docker compose restart api
```

The run finishes anyway. Execution has its own lifecycle, separate from the API's.

**5. Read a finished run.** Use a run ID from the watch pane:

```bash
curl -s localhost:8000/runs/run-25690114 | python3 -m json.tool
```

The response has the status, the attempt with its turns, tokens, and cost, the check
result with expected and reported figures side by side, and the report itself.

**6. Look at what was written to Postgres.**

```bash
./tables.sh
```

This prints the `runs` table, one row per requested task, and the `attempts` table, one
row per execution with its turns, tokens, cost, and seconds. Pass a run ID to see that
run's events in order:

```bash
./tables.sh run-941cd76f
```

```text
    at    | attempt |      type      |                   summary
----------+---------+----------------+----------------------------------------------
 00:11:26 |         | submitted      |
 00:11:36 |       1 | claimed        | acct-001 by worker-1
 00:11:38 |       1 | tool_call      | Read: /work/run-941cd76f-attempt-1/usage.csv
 00:11:42 |       1 | tool_call      | Bash: python3 << 'EOF'
 00:11:45 |       1 | tool_call      | Write: /work/run-941cd76f-attempt-1/summary.md
 00:11:50 |       1 | agent_finished | success, 6 turns, 35,374 tokens in, $0.021
 00:11:50 |       1 | finished       | check passed, every figure matches the input
```

That is a real run with the agent's text lines and two file reads left out for space.
The gap between `submitted` and `claimed` is the time the run waited on the queue.

**7. Submit ten runs at once.**

```bash
python3 submit.py --count 10
```

Ten run IDs come back in well under a second. The watch pane shows one run `running`
and the rest `queued`. The same worker takes them one after another.

```text
queued 6   running 1   finished 4   failed 0   cost $0.080

run           account   status    check   worker        turns  time   cost
run-25690114  acct-001  finished  passed  worker-1      8      18s    $0.026
run-aa8f7575  acct-002  finished  passed  worker-1      7      13s    $0.016
```

**8. Do the arithmetic.** In our validation run a run took about 16 seconds, so one
worker clears about 230 runs an hour. A thousand accounts would take more than four
hours. That backlog is where Module 2 starts.

## Files to follow

Read them in this order. Numbered comments in the code mark the steps.

| File | What to read for |
|---|---|
| `api.py` | `submit_run`: save the run, queue its ID, return. Three steps |
| `worker.py` | `main` receives a run ID. `execute_run` claims it, prepares a working directory, runs the agent under a deadline, saves the outputs, checks them, and records the outcome. `check_report` is the completion check |
| `agent.py` | The prompt, the SDK options, and the loop that reports each agent step to the worker |
| `schema.sql` | Three tables: `runs`, `attempts`, `events` |
| `compose.yaml` | Four services. Only the worker receives the model key |
| `submit.py`, `watch.py` | Small clients for the API |
| `tables.sh` | Prints the `runs` and `attempts` tables, or one run's events |

The API and the worker are asynchronous. `await` marks a wait where the process can do
other work. The API serves other requests while one waits on Postgres. The worker follows
the agent while a deadline runs alongside it.

## What to look for

- **Submission does not wait for execution.** A run ID comes back in milliseconds, even
  with no worker running. The durable record is written before the response.
- **The queue holds work until a worker is free.** The message carries only the run ID.
  Everything else about the run lives in Postgres.
- **A run is claimed once.** The `UPDATE ... WHERE status = 'queued'` in `execute_run`
  succeeds for one claimant only.
- **The task travels with the assignment.** The worker writes `task.json` with the inputs,
  expected outputs, and limits. The agent reads its limits from that file, not from the
  original request.
- **Limits are enforced, not suggested.** Fifteen turns and 25 cents per run inside the
  agent, and a 180-second deadline in the worker.
- **Records and outputs live in different places.** Postgres holds status, usage, cost,
  the check decision, and the path to the report. The report files sit on the artifacts
  volume, the way run records and saved outputs map to a database and object storage.
- **Completion is checked.** A finished run records whether the recomputed figures equal
  the reported ones. Both sets are stored as evidence.
- **One worker, many runs.** The worker finishes a run, returns to the queue, and takes
  the next. Each run gets a fresh working directory, deleted afterwards.

## The execution boundary

In this demo the worker's container is the sandbox. That is a common design, and it is
worth being exact about what it does.

- It keeps the agent's commands away from your machine, the API, and the database
  server's files. The agent runs as a regular user with CPU and memory limits, and nothing
  mounts the Docker socket.
- It does not hide the worker from the agent. The agent's shell can read the worker's
  environment, including the database URL, the queue URL, and the model key. It can reach
  Postgres and Redis over the network. It shares the container's filesystem with earlier
  runs, apart from the working directory that is deleted each time.

Production systems that place the agent inside the execution environment, such as a
microVM per session or a container per task, make that environment serve one run, destroy
it afterwards, and keep control-plane credentials out of it. A long-lived worker does none
of those by default.

## Cleanup

Stop everything and delete the run records, the queue, and the saved reports:

```bash
docker compose down -v
```

## Troubleshooting

- **`env file ../../.env not found`**: create `.env` in the repository root as shown in Setup.
- **Port 8000 is taken**: start with `API_PORT=8001 docker compose up -d --build api` and
  run the clients with `API_URL=http://localhost:8001`.
- **A run fails with an authentication error**: check the key in `.env`, then
  `docker compose up -d --force-recreate worker`.
- **You changed the code and nothing changed**: rebuild with `docker compose up -d --build api worker`.
  Both services share one image.
- **A run is stuck in `running` after you stopped the worker**: `docker compose down -v` and start again.
