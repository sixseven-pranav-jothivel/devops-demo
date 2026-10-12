"""Declarative checks on demo/scenarios.yaml and its fixtures (read-only)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from demo.engine import MARKER_PREFIX, RUNTIME_ALLOW_KEY, RUNTIME_ENV_KEY, Scenario, load_config

REPO = Path(__file__).resolve().parents[2]
CONFIG = load_config(REPO)
SCENARIOS = list(CONFIG.scenarios)
EXPECTED_IDS = {
    "clean",
    "bad_lint",
    "bad_format",
    "bad_typecheck",
    "bad_unit_test",
    "bad_integration_test",
    "bad_coverage",
    "bad_security",
    "bad_dependency",
    "bad_container",
    "slow_api",
    "unhealthy_health",
    "bad_deployment",
}
PROTECTED = ("config/", ".trivyignore", "scripts/quality_policy.py", ".github/", "Makefile")

pytestmark = pytest.mark.demo


def test_the_required_scenarios_exist() -> None:
    assert {s.id for s in SCENARIOS} == EXPECTED_IDS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_scenario_declares_the_required_metadata(scenario: Scenario) -> None:
    assert scenario.name
    assert scenario.description
    assert scenario.remediation
    assert scenario.reproduce, "reproduce commands are required"
    assert isinstance(scenario.deployment_allowed, bool)
    assert scenario.expected["gate"]
    assert scenario.expected["command"]
    assert scenario.expected["stage"] in {"pr-ci", "image-build", "post-deploy"}
    if scenario.id != "clean":
        assert scenario.expected["output_contains"], "a failure signature is required"
        assert scenario.files, "affected files are required"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_inserted_hunk_carries_the_scenario_marker(scenario: Scenario) -> None:
    marker = MARKER_PREFIX + scenario.id
    for op in scenario.patch:
        text = op.new if op.kind in ("replace_text", "replace_regex") else (op.content or "")
        if op.source:
            text = (REPO / "demo" / "fixtures" / scenario.id / op.source).read_text("utf-8")
        assert marker in text, f"{scenario.id}: {op.file} hunk lacks {marker}"


def test_only_clean_and_runtime_scenarios_are_deployable() -> None:
    allowed = {s.id for s in SCENARIOS if s.deployment_allowed}
    assert allowed == {"clean", "slow_api", "unhealthy_health", "bad_deployment"}
    for s in SCENARIOS:
        if s.category != "runtime" and s.id != "clean":
            assert not s.deployment_allowed, f"{s.id} fails a pre-deploy gate"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_no_scenario_touches_scanning_policy_or_pipeline(scenario: Scenario) -> None:
    for rel in scenario.files:
        assert not rel.startswith(PROTECTED), f"{scenario.id} edits protected file {rel}"
    for op in scenario.patch:
        if op.file == "pyproject.toml":
            assert "[tool" not in op.new
            assert "ignore" not in op.new.lower()


def test_dependency_scenario_never_adds_an_ignore() -> None:
    scenario = CONFIG.get("bad_dependency")
    blob = " ".join(op.new for op in scenario.patch).lower()
    assert "ignore" not in blob
    assert "--ignore-vuln" not in blob


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS if s.category == "runtime"], ids=lambda s: s.id
)
def test_runtime_scenarios_are_two_key_env_gated_and_never_read_request_data(
    scenario: Scenario,
) -> None:
    assert scenario.runtime_env == {RUNTIME_ENV_KEY: scenario.id, RUNTIME_ALLOW_KEY: "true"}
    assert scenario.expected["stage"] == "post-deploy"
    added = "\n".join(
        line for op in scenario.patch for line in op.new.splitlines() if line not in op.old
    )
    assert RUNTIME_ENV_KEY in added
    assert RUNTIME_ALLOW_KEY in added
    assert f'== "{scenario.id}"' in added
    for forbidden in (
        "request",
        "Request",
        "headers",
        "query_params",
        "cookies",
        "Header(",
        "Query(",
    ):
        assert forbidden not in added, f"{scenario.id} reads {forbidden}"


def test_runtime_failures_are_disabled_by_default() -> None:
    assert CONFIG.allow_runtime_failures is False


def test_fixture_hashes_match_and_fixtures_are_scanner_safe() -> None:
    fixtures = REPO / "demo" / "fixtures"
    for scenario in SCENARIOS:
        for op in scenario.patch:
            if op.source:
                data = (fixtures / scenario.id / op.source).read_bytes()
                if op.sha256:
                    assert hashlib.sha256(data).hexdigest() == op.sha256
    names = [p.name for p in fixtures.rglob("*") if p.is_file()]
    # Trivy's pip analyzer matches requirements.txt exactly; ruff/mypy would lint .py fixtures.
    assert "requirements.txt" not in names
    assert not [n for n in names if n.endswith(".py")]


def test_fixtures_contain_no_secret_shaped_strings() -> None:
    pattern = re.compile(
        r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|xox[bp]-"
    )
    for path in (REPO / "demo").rglob("*"):
        if path.is_file() and path.suffix in {".txt", ".yaml", ".md", ".py"}:
            assert not pattern.search(path.read_text("utf-8")), path


def test_dockerignore_allow_list_keeps_demo_out_of_the_image() -> None:
    dockerignore = REPO / ".dockerignore"
    if not dockerignore.is_file():
        pytest.skip(".dockerignore not present yet")
    lines = [ln.strip() for ln in dockerignore.read_text("utf-8").splitlines()]
    rules = [ln for ln in lines if ln and not ln.startswith("#")]
    assert rules[0] == "*", "allow-list must start by excluding everything"
    reincluded = {ln[1:].rstrip("/") for ln in rules if ln.startswith("!")}
    assert not reincluded & {"demo", "tests", ".demo-state", "load-tests", "docs"}
    assert not any(r.startswith("demo") for r in reincluded)
