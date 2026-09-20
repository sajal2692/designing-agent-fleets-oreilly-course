# Designing Agent Fleets

Companion repository for the O'Reilly live course **Designing Agent Fleets: Build the
Control Plane That Runs Many AI Agents at Once**, taught by Sajal Sharma.

The course has two instructor-led demos that run locally with Docker Compose. You can
follow both in class without installing anything. This repository lets you read the
code during the session and reproduce the runs afterwards.

## Repository map

```text
demos/01_agent_worker/   Demo 1: an API, a queue, and one worker that runs an agent
demos/02_agent_fleet/    Demo 2: a controller, a pool of workers, fleet limits, and a dashboard
tests/                   Offline checks, a fake agent, and an end-to-end verifier for each demo
pyproject.toml           Pinned dependencies for the tests
.env.example             Template for the Anthropic API key
```

Each demo directory has its own README with setup, the full walkthrough, the files to
read, what to look for, cleanup, and troubleshooting.

## The demos

| | Demo 1: Agent Execution Inside a Worker | Demo 2: Running a Fleet of Agents |
|---|---|---|
| Question | What changes when agent execution moves out of the application and into a worker? | What does it take to run many agent runs across many workers? |
| Shows | A run submitted to an API, handed to a worker through a queue, executed by an agent in the worker's container, and checked. Then ten runs served by one worker | A controller that keeps a pool of workers at a target size, a fleet-wide limit on runs in progress, a killed worker's run finished by another worker, scale-down by draining, and a live dashboard |
| Start here | [demos/01_agent_worker](demos/01_agent_worker/README.md) | [demos/02_agent_fleet](demos/02_agent_fleet/README.md) |

## Requirements

- Docker Desktop
- Python 3.12 or newer
- An Anthropic API key. Both demos use Claude Haiku 4.5 at about 2 cents per run. A full Demo 1
  walkthrough costs about 22 cents and a full Demo 2 walkthrough about $1

Copy `.env.example` to `.env` and add your key. Only the worker container receives it.

## Tests

The tests use [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run python tests/run_demo_1.py
uv run python tests/run_demo_2.py
```

`pytest` runs the offline checks: the completion check, the synthetic accounts, the fake
agent, and the controller's planning logic. Each `run_demo` script starts its demo in its
own Compose project with the fake agent, drives it the way the walkthrough does, and asserts
each claim. They call no model and cost nothing. Add `--live` to use the real agent and
print time, cost, and memory. `run_demo_2.py --live --quick` runs only the failure and
limit steps, for under a dollar.
