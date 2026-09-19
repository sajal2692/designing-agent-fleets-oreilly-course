#!/usr/bin/env bash
# Show what the demo wrote to Postgres.
#
#   ./tables.sh              the runs and attempts tables
#   ./tables.sh run-1a2b3c4d that run's events, in order
set -euo pipefail

query() { docker compose exec -T postgres psql -U postgres -d fleet -c "$1"; }

if [ $# -eq 0 ]; then
  echo "runs: one row per requested task"
  query "SELECT id, account_id, status, check_result->>'passed' AS check_passed, report_path
         FROM runs ORDER BY created_at"
  echo "attempts: one row per execution of a run"
  query "SELECT run_id, number, worker_id, status, turns, input_tokens, output_tokens,
                round(cost_usd::numeric, 4) AS cost_usd,
                round(extract(epoch FROM finished_at - started_at)) AS seconds, error
         FROM attempts ORDER BY started_at"
elif [[ "$1" =~ ^run-[0-9a-f]+$ ]]; then
  echo "events for $1: what happened, in order"
  query "SELECT to_char(at, 'HH24:MI:SS') AS at, attempt, type, left(detail->>'summary', 90) AS summary
         FROM events WHERE run_id = '$1' ORDER BY id"
else
  echo "usage: ./tables.sh [run-id]" >&2
  exit 1
fi
