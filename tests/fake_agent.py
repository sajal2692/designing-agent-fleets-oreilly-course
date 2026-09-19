"""A stand-in for agent.py that calls no model. The worker loads it when
AGENT_MODULE=fake_agent. It waits, computes the correct figures, and writes the same
two files the real agent writes, so the whole run path can be exercised offline."""

import asyncio
import json
import os
from pathlib import Path

import pandas as pd

SECONDS = float(os.environ.get("FAKE_AGENT_SECONDS", "2"))


async def run_agent(workdir: Path):
    yield {"type": "tool_call", "summary": "Bash: python analyze.py",
           "tool": "Bash", "input": {"command": "python analyze.py"}}
    await asyncio.sleep(SECONDS)
    usage = pd.read_csv(workdir / "usage.csv")
    report = {
        "row_count": int(len(usage)),
        "total_units": int(usage["units"].sum()),
        "top_product": str(usage.groupby("product")["units"].sum().idxmax()),
        "peak_day": str(usage.groupby("date")["units"].sum().idxmax()),
    }
    (workdir / "report.json").write_text(json.dumps(report, indent=2))
    (workdir / "summary.md").write_text("Written by the fake agent.\n")
    yield {"type": "agent_finished", "summary": "success, 1 turn, no model calls", "ok": True,
           "turns": 1, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
