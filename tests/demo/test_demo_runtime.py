"""Behavioural proof that the runtime faults are inert unless BOTH env keys are set."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from demo.engine import Engine

pytestmark = pytest.mark.demo

BOTH = {"STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES": "true"}


def run_snippet(root: Path, code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKFLOW_DEMO")}
    clean_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=root,
        env={**clean_env, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def injected(repo: Path, engine: Engine):  # type: ignore[no-untyped-def]
    def _inject(scenario_id: str) -> Path:
        engine.inject(scenario_id, allow_runtime=True)
        return repo

    return _inject


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "False"),
        ({"STOCKFLOW_DEMO_FAULT": "slow_api"}, "False"),
        (BOTH, "False"),
        ({"STOCKFLOW_DEMO_FAULT": "unhealthy_health", **BOTH}, "False"),
        ({"STOCKFLOW_DEMO_FAULT": "slow_api", **BOTH}, "True"),
    ],
)
def test_slow_api_gate_needs_the_exact_id_and_the_allow_flag(injected, env, expected) -> None:  # type: ignore[no-untyped-def]
    root = injected("slow_api")
    proc = run_snippet(root, "import app.api.products as p; print(p._demo_fault_active())", env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


HEALTH_SNIPPET = """
    from fastapi.testclient import TestClient
    from app.main import create_app
    print(TestClient(create_app()).get("/health").status_code)
"""


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "200"),
        ({"STOCKFLOW_DEMO_FAULT": "unhealthy_health"}, "200"),
        ({"STOCKFLOW_DEMO_FAULT": "slow_api", **BOTH}, "200"),
        ({"STOCKFLOW_DEMO_FAULT": "unhealthy_health", **BOTH}, "503"),
    ],
)
def test_unhealthy_health_gate(injected, env, expected) -> None:  # type: ignore[no-untyped-def]
    root = injected("unhealthy_health")
    proc = run_snippet(root, HEALTH_SNIPPET, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_unhealthy_health_ignores_request_headers_and_query_params(injected) -> None:  # type: ignore[no-untyped-def]
    root = injected("unhealthy_health")
    code = """
        from fastapi.testclient import TestClient
        from app.main import create_app
        c = TestClient(create_app())
        r = c.get("/health?STOCKFLOW_DEMO_FAULT=unhealthy_health",
                  headers={"STOCKFLOW_DEMO_FAULT": "unhealthy_health",
                           "X-Demo-Fault": "unhealthy_health"})
        print(r.status_code)
    """
    proc = run_snippet(root, code, {})
    assert proc.stdout.strip() == "200", proc.stderr


@pytest.mark.parametrize(
    ("env", "crashes"),
    [
        ({}, False),
        ({"STOCKFLOW_DEMO_FAULT": "bad_deployment"}, False),
        (BOTH, False),
        ({"STOCKFLOW_DEMO_FAULT": "bad_deployment", **BOTH}, True),
    ],
)
def test_bad_deployment_crashes_only_with_both_keys(injected, env, crashes) -> None:  # type: ignore[no-untyped-def]
    root = injected("bad_deployment")
    proc = run_snippet(root, "from app.main import create_app; create_app(); print('started')", env)
    if crashes:
        assert proc.returncode != 0
        assert "simulated startup failure" in proc.stderr
    else:
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "started"


def test_clean_baseline_ignores_the_fault_env_entirely(repo: Path) -> None:
    env = {"STOCKFLOW_DEMO_FAULT": "bad_deployment", **BOTH}
    proc = run_snippet(repo, "from app.main import create_app; create_app(); print('started')", env)
    assert proc.returncode == 0, proc.stderr
    assert "STOCKFLOW_DEMO" not in "".join(
        p.read_text("utf-8") for p in (repo / "app").rglob("*.py")
    )
