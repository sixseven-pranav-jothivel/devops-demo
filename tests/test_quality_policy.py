"""pyproject coverage settings equal config/quality-policy.yaml; the policy tool is healthy."""

from __future__ import annotations

import copy
import importlib.util
import itertools
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
POLICY_FILE = ROOT / "config" / "quality-policy.yaml"
TOOL = ROOT / "scripts" / "quality_policy.py"
PERFORMANCE_JS = ROOT / "load-tests" / "performance.js"
COVERAGE_FLOOR = 85


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("quality_policy", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["quality_policy"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    return yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ the mirror
def test_coverage_fail_under_in_pyproject_equals_the_policy(
    policy: dict[str, Any], pyproject: dict[str, Any]
) -> None:
    assert pyproject["tool"]["coverage"]["report"]["fail_under"] == policy["coverage"]["fail_under"]


def test_branch_coverage_and_source_match_the_policy(
    policy: dict[str, Any], pyproject: dict[str, Any]
) -> None:
    run = pyproject["tool"]["coverage"]["run"]
    assert run["branch"] is policy["coverage"]["branch"] is True
    assert sorted(run["source"]) == sorted(policy["coverage"]["source"])


def test_the_coverage_threshold_is_not_below_the_agreed_floor(policy: dict[str, Any]) -> None:
    assert policy["coverage"]["fail_under"] >= COVERAGE_FLOOR


def test_pytest_does_not_hard_code_a_second_threshold(pyproject: dict[str, Any]) -> None:
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert "--cov-fail-under" not in addopts


def test_pytest_markers_used_for_ci_splitting_are_declared(pyproject: dict[str, Any]) -> None:
    markers = {
        m.split(":")[0].strip() for m in pyproject["tool"]["pytest"]["ini_options"]["markers"]
    }
    assert {"unit", "integration", "concurrency", "demo"} <= markers


def test_pytest_runs_with_strict_markers(pyproject: dict[str, Any]) -> None:
    assert "--strict-markers" in pyproject["tool"]["pytest"]["ini_options"]["addopts"]


# ------------------------------------------------------------------ the tool on the real repository
def test_the_policy_check_is_clean_on_the_repository() -> None:
    tool = load_tool()
    assert tool.run_check(ROOT) == []


def test_cli_get_prints_the_threshold(policy: dict[str, Any]) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(TOOL), "--root", str(ROOT), "get", "coverage.fail_under"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(policy["coverage"]["fail_under"])


def test_cli_check_exit_code_is_zero_on_the_repository() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(TOOL), "--root", str(ROOT), "check"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------ the mirror is actually enforced
@pytest.fixture
def scratch_root(tmp_path: Path) -> Path:
    """A copy of the files `check` reads, so mutations never touch the real repository."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    for name in ("pyproject.toml", ".python-version", ".trivyignore"):
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def test_a_drifting_pyproject_threshold_is_detected(scratch_root: Path) -> None:
    tool = load_tool()
    before = tool.run_check(scratch_root)
    pyproject = scratch_root / "pyproject.toml"
    pyproject.write_text(
        re.sub(r"fail_under = \d+", "fail_under = 60", pyproject.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    new_problems = [p for p in tool.run_check(scratch_root) if p not in before]
    assert any("fail_under" in p and "60" in p for p in new_problems), new_problems


def test_a_weakened_policy_threshold_is_detected(policy: dict[str, Any]) -> None:
    tool = load_tool()
    weakened = copy.deepcopy(policy)
    weakened["coverage"]["fail_under"] = 70
    weakened["load_test"]["p95_ms"] = 2000
    problems = tool.check_floors(weakened)
    assert any("coverage.fail_under" in p for p in problems)
    assert any("load_test.p95_ms" in p for p in problems)


# ---------------------------------------------------------- load-test defaults mirror the policy
def js_default(name: str) -> str:
    match = re.search(
        rf"__ENV\.{name}\s*\|\|\s*'([^']+)'", PERFORMANCE_JS.read_text(encoding="utf-8")
    )
    assert match, f"{name} default not found in {PERFORMANCE_JS.name}"
    return match.group(1)


def test_performance_defaults_equal_the_policy(policy: dict[str, Any]) -> None:
    assert float(js_default("P95_MS")) == float(policy["load_test"]["p95_ms"])
    assert float(js_default("ERROR_RATE_MAX")) == float(policy["load_test"]["error_rate_max"])


def test_policy_k6_args_use_the_variables_performance_js_reads(policy: dict[str, Any]) -> None:
    tool = load_tool()
    args = tool.build_args(policy, "k6")
    text = PERFORMANCE_JS.read_text(encoding="utf-8")
    env_names = [arg.split("=")[0] for flag, arg in itertools.pairwise(args) if flag == "-e"]
    assert env_names
    for variable in env_names:
        assert f"__ENV.{variable}" in text
    joined = " ".join(args)
    assert f"P95_MS={policy['load_test']['p95_ms']}" in joined
    assert f"ERROR_RATE_MAX={policy['load_test']['error_rate_max']}" in joined
    assert "--vus" in args
    assert "--duration" in args


# ------------------------------------------------------------------ job-level write permissions
def test_write_permissions_are_allowed_only_for_the_reviewed_workflows(
    policy: dict[str, Any],
) -> None:
    allowed = policy["actions"]["allowed_job_write_permissions"]
    assert allowed == {
        "packages": ["release.yml", "pr-preview.yml"],
        "pull-requests": ["pr-preview.yml"],
    }


def test_a_new_write_permission_or_workflow_is_rejected() -> None:
    tool = load_tool()
    actions = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))["actions"]
    workflow = (
        "name: x\non: workflow_dispatch\npermissions:\n  contents: read\n"
        "jobs:\n  j:\n    runs-on: ubuntu-24.04\n    permissions:\n      {perm}: write\n"
        "    steps:\n      - run: echo ok\n"
    )
    assert tool.check_workflow("pr-preview.yml", workflow.format(perm="packages"), actions) == []
    assert (
        tool.check_workflow("pr-preview.yml", workflow.format(perm="pull-requests"), actions) == []
    )
    assert tool.check_workflow("release.yml", workflow.format(perm="pull-requests"), actions)
    assert tool.check_workflow("ci.yml", workflow.format(perm="packages"), actions)
    assert tool.check_workflow("pr-preview.yml", workflow.format(perm="contents"), actions)
