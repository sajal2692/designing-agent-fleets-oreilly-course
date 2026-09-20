"""Print the queue and the run table once a second. Stop with Ctrl-C."""

import json
import os
import time
import urllib.request
from collections import Counter
from datetime import datetime

API_URL = os.environ.get("API_URL", "http://localhost:8000")


def seconds_between(start, end):
    if not start:
        return ""
    finish = datetime.fromisoformat(end) if end else datetime.now().astimezone()
    return f"{(finish - datetime.fromisoformat(start)).total_seconds():.0f}s"


while True:
    try:
        with urllib.request.urlopen(f"{API_URL}/runs") as response:
            runs = json.load(response)
        counts = Counter(run["status"] for run in runs)
        cost = sum(run["cost_usd"] or 0 for run in runs)
        lines = [
            f"queued {counts['queued']}   running {counts['running'] + counts['checking']}   "
            f"accepted {counts['accepted']}   failed {counts['failed']}   cost ${cost:.3f}",
            "",
            f"{'run':<14}{'account':<10}{'status':<10}{'try':<5}{'worker':<14}{'turns':<7}{'time':<7}cost",
        ]
        for run in runs:
            lines.append(
                f"{run['id']:<14}{run['account_id']:<10}{run['status']:<10}{run['attempt'] or '':<5}"
                f"{run['worker_id'] or '':<14}{run['turns'] or '':<7}"
                f"{seconds_between(run['started_at'], run['finished_at']):<7}"
                f"{'$%.3f' % run['cost_usd'] if run['cost_usd'] else ''}"
            )
    except OSError:
        lines = ["API not reachable, retrying..."]

    print("\033[2J\033[H" + "\n".join(lines), flush=True)  # clear the screen, then draw
    try:
        time.sleep(1)
    except KeyboardInterrupt:
        raise SystemExit  # Ctrl-C leaves quietly
