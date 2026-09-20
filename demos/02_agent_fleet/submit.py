"""Submit one run, or a batch, and print what the API returns."""

import argparse
import json
import os
import time
import urllib.request

API_URL = os.environ.get("API_URL", "http://localhost:8000")

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("account", nargs="?", default="acct-001", help="account to report on")
parser.add_argument("--count", type=int, help="submit this many runs, cycling through the 40 accounts")
args = parser.parse_args()

accounts = [f"acct-{n % 40 + 1:03d}" for n in range(args.count)] if args.count else [args.account]
for account_id in accounts:
    request = urllib.request.Request(
        f"{API_URL}/runs",
        data=json.dumps({"account_id": account_id}).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request) as response:
        run = json.load(response)
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(f"{run['run_id']}  {account_id}  {run['status']}  returned in {elapsed_ms:.0f} ms")
