#!/usr/bin/env bash
# Show what the fleet wrote to Postgres.
#
#   ./tables.sh              workers, settings, runs, and attempts
#   ./tables.sh run-1a2b3c4d that run's attempts and events, in order
set -euo pipefail

query() { docker compose exec -T postgres psql -U postgres -d fleet -c "$1"; }

if [ $# -eq 0 ]; then
  echo "workers serving now, of all the workers the controller has started"
  query "SELECT id, status, drain, container, current_run, runs_done,
                round(extract(epoch FROM now() - heartbeat_at)) AS seconds_since_heartbeat
         FROM workers WHERE status <> 'gone' ORDER BY substring(id FROM 8)::int"
  query "SELECT count(*) AS workers_that_have_left FROM workers WHERE status = 'gone'"
  echo "settings: the fleet's targets and limits"
  query "SELECT key, value FROM settings ORDER BY key"
  echo "runs by status"
  query "SELECT status, count(*) FROM runs GROUP BY status ORDER BY status"
  echo "attempts that did not finish normally"
  query "SELECT run_id, number, worker_id, status, error FROM attempts
         WHERE status IN ('lost', 'failed') ORDER BY started_at"
elif [[ "$1" =~ ^run-[0-9a-f]+$ ]]; then
  echo "attempts for $1"
  query "SELECT number, worker_id, status, turns, round(cost_usd::numeric, 4) AS cost_usd,
                round(extract(epoch FROM finished_at - started_at)) AS seconds, error
         FROM attempts WHERE run_id = '$1' ORDER BY number"
  echo "events for $1: what happened, in order"
  query "SELECT to_char(at, 'HH24:MI:SS') AS at, attempt, source, type, left(detail->>'summary', 80) AS summary
         FROM events WHERE run_id = '$1' ORDER BY id"
else
  echo "usage: ./tables.sh [run-id]" >&2
  exit 1
fi
