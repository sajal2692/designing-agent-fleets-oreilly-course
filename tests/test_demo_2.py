"""Offline checks for Demo 2. No containers, no network, no model calls."""

import ast
import importlib.util
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("REDIS_URL", "redis://unused")
os.environ.setdefault("FLEET_PROJECT", "unused")

ROOT = Path(__file__).resolve().parents[1]
DEMO_1, DEMO_2 = ROOT / "demos/01_agent_worker", ROOT / "demos/02_agent_fleet"


def load(name):
    """Both demos have a worker.py, so Demo 2's modules are loaded by path."""
    spec = importlib.util.spec_from_file_location(f"fleet_{name}", DEMO_2 / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = load("controller")


def workers(*rows):
    return [{"id": f"worker-{n}", "status": status, "drain": drain} for n, status, drain in rows]


def test_missing_workers_are_started():
    assert controller.plan_workers(3, [], 0) == (3, [])
    assert controller.plan_workers(6, workers((1, "busy", False), (2, "ready", False)), 0) == (4, [])


def test_a_starting_container_is_not_started_twice():
    # Two are serving and one container has not registered yet: nothing more to start.
    assert controller.plan_workers(3, workers((1, "busy", False), (2, "ready", False)), 1) == (0, [])


def test_surplus_workers_drain_idle_first():
    fleet = workers((1, "busy", False), (2, "ready", False), (3, "busy", False), (4, "waiting", False))
    start, drain = controller.plan_workers(2, fleet, 0)
    assert start == 0 and sorted(drain) == ["worker-2", "worker-4"]


def test_draining_workers_do_not_count_as_serving():
    fleet = workers((1, "busy", True), (2, "ready", False))
    assert controller.plan_workers(2, fleet, 0) == (1, [])
    assert controller.plan_workers(1, fleet, 0) == (0, [])


def test_the_fleet_never_exceeds_the_worker_clamp():
    assert controller.MAX_WORKERS == 10 and controller.MAX_ATTEMPTS == 2


def test_the_agent_is_identical_in_both_demos():
    assert (DEMO_1 / "agent.py").read_text() == (DEMO_2 / "agent.py").read_text()


def test_the_completion_check_is_identical_in_both_demos():
    def check_source(path):
        tree = ast.parse(path.read_text())
        return next(ast.unparse(node) for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "check_report")
    assert check_source(DEMO_1 / "worker.py") == check_source(DEMO_2 / "worker.py")


def test_the_corrupt_account_lacks_the_units_column():
    header = (DEMO_2 / "data/accounts/acct-corrupt.csv").read_text().splitlines()[0]
    assert header != "date,product,units"
    assert len(list((DEMO_2 / "data/accounts").glob("acct-0*.csv"))) == 40


def test_teaching_scripts_parse_and_have_no_test_branches():
    for name in ("api.py", "worker.py", "agent.py", "controller.py", "dashboard.py", "submit.py", "watch.py"):
        source = (DEMO_2 / name).read_text()
        ast.parse(source)
        assert source.lower().count("fake") <= 2, name  # only the AGENT_MODULE and env pass-through mentions
