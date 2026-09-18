# Designing Agent Fleets

Companion repository for the O'Reilly live course **Designing Agent Fleets: Build the
Control Plane That Runs Many AI Agents at Once**, taught by Sajal Sharma.

The course has two instructor-led demos that run locally with Docker Compose. You can
follow both in class without installing anything. This repository lets you read the
code during the session and reproduce the runs afterwards.

## The demos

| | Demo 1: Agent Execution Inside a Worker | Demo 2: Running a Fleet of Agents |
|---|---|---|
| Question | What changes when agent execution moves out of the application and into a worker? | What does it take to run many agent runs across many workers? |
| Shows | The same agent run inside an API request, then handed to a worker through a queue and executed in its own sandbox | A controller that keeps workers at a target count, recovers runs from lost workers, cleans up sandboxes, and reports the fleet on a dashboard |

## Status

This repository is in development. Demo code, requirements, and setup instructions
will be added before the first session.
