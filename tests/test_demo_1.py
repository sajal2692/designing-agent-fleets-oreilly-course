"""Offline checks for Demo 1. No containers, no network, no model calls."""

import ast
import csv
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("REDIS_URL", "redis://unused")

import worker  # noqa: E402  (needs the two variables above at import time)

DEMO = Path(__file__).resolve().parents[1] / "demos/01_agent_worker"
ACCOUNTS = sorted((DEMO / "data/accounts").glob("acct-*.csv"))
GOOD = {"row_count": 90, "total_units": 0, "top_product": "", "peak_day": ""}


def expected_for(path):
    return worker.check_report(path, {})["expected"]


def test_there_are_forty_accounts_of_ninety_rows():
    assert len(ACCOUNTS) == 40
    for path in ACCOUNTS:
        with open(path) as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 90 and set(rows[0]) == {"date", "product", "units"}


def test_every_account_has_one_top_product_and_one_peak_day():
    for path in ACCOUNTS:
        by_product, by_day = {}, {}
        with open(path) as handle:
            for row in csv.DictReader(handle):
                by_product[row["product"]] = by_product.get(row["product"], 0) + int(row["units"])
                by_day[row["date"]] = by_day.get(row["date"], 0) + int(row["units"])
        for totals in (by_product, by_day):
            top_two = sorted(totals.values())[-2:]
            assert top_two[0] != top_two[1], f"tie in {path.name}"


def test_a_correct_report_passes():
    path = ACCOUNTS[0]
    check = worker.check_report(path, expected_for(path))
    assert check["passed"] and check["reported"] == check["expected"]


def test_a_wrong_figure_fails_and_keeps_the_evidence():
    path = ACCOUNTS[0]
    report = expected_for(path) | {"total_units": 1}
    check = worker.check_report(path, report)
    assert not check["passed"]
    assert check["reported"]["total_units"] == 1 and check["expected"]["total_units"] != 1


def test_missing_keys_and_non_objects_fail():
    path = ACCOUNTS[0]
    assert not worker.check_report(path, {"row_count": 90})["passed"]
    assert not worker.check_report(path, ["not", "an", "object"])["passed"]


def test_figures_written_as_whole_floats_still_pass():
    path = ACCOUNTS[0]
    report = expected_for(path)
    report["total_units"] = float(report["total_units"])
    assert worker.check_report(path, report)["passed"]


def test_fake_agent_writes_a_report_the_check_accepts(tmp_path):
    import asyncio
    import json
    import shutil
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    os.environ["FAKE_AGENT_SECONDS"] = "0"
    import fake_agent

    shutil.copy(ACCOUNTS[1], tmp_path / "usage.csv")

    async def collect():
        return [event async for event in fake_agent.run_agent(tmp_path)]

    events = asyncio.run(collect())
    assert events[-1]["type"] == "agent_finished" and events[-1]["ok"]
    report = json.loads((tmp_path / "report.json").read_text())
    assert worker.check_report(tmp_path / "usage.csv", report)["passed"]
    assert (tmp_path / "summary.md").exists()


def test_teaching_scripts_parse_and_have_no_test_branches():
    for name in ("api.py", "worker.py", "agent.py", "submit.py", "watch.py"):
        source = (DEMO / name).read_text()
        ast.parse(source)
        assert "fake" not in source.lower() or name == "worker.py", name
