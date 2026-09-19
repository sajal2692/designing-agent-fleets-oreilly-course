# Designing Agent Fleets

Companion repository for the O'Reilly live course **Designing Agent Fleets: Build the
Control Plane That Runs Many AI Agents at Once**, taught by Sajal Sharma.

The course has two instructor-led demos that run locally with Docker Compose. You can
follow both in class without installing anything. This repository lets you read the
code during the session and reproduce the runs afterwards.

## Repository map

```text
demos/01_agent_worker/   Demo 1: an API, a queue, and one worker that runs an agent
tests/                   Offline checks, a fake agent, and an end-to-end verifier
pyproject.toml           Pinned dependencies for the tests
.env.example             Template for the Anthropic API key
```

Each demo directory has its own README with setup, the full walkthrough, the files to
read, what to look for, cleanup, and troubleshooting.

## The demos

| | Demo 1: Agent Execution Inside a Worker | Demo 2: Running a Fleet of Agents |
|---|---|---|
| Question | What changes when agent execution moves out of the application and into a worker? | What does it take to run many agent runs across many workers? |
| Shows | A run submitted to an API, handed to a worker through a queue, executed by an agent in the worker's container, and checked. Then ten runs served by one worker | In development |
| Start here | [demos/01_agent_worker](demos/01_agent_worker/README.md) | |

## Requirements

- Docker Desktop
- Python 3.12 or newer
- An Anthropic API key. Demo 1 uses Claude Haiku 4.5 and costs about 2 cents per run

Copy `.env.example` to `.env` and add your key. Only the worker container receives it.

## Tests

The tests use [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run python tests/run_demo_1.py
```

`pytest` runs the offline checks: the completion check, the synthetic accounts, and the
fake agent. `run_demo_1.py` starts the whole demo in its own Compose project with the
fake agent, walks the live sequence, and asserts each claim. It calls no model and costs
nothing. Add `--live --count 10` to run the same sequence with the real agent and print
seconds per run, cost, and worker memory.
