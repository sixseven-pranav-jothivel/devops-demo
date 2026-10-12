"""Structural guarantees of .github/workflows/pr-preview.yml (the parts a unit test can prove)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "pr-preview.yml"


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps(wf: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return wf["jobs"][job]["steps"]


def step_ids(wf: dict[str, Any], job: str) -> list[str]:
    return [s.get("id", "") for s in steps(wf, job)]


def test_never_pull_request_target(wf: dict[str, Any]) -> None:
    triggers = wf.get(True, wf.get("on"))
    assert set(triggers) == {"pull_request"}
    assert set(triggers["pull_request"]["types"]) == {
        "labeled",
        "unlabeled",
        "synchronize",
        "reopened",
        "closed",
    }


def test_cleanup_events_have_their_own_group_and_never_cancel(wf: dict[str, Any]) -> None:
    group = wf["concurrency"]["group"]
    assert "-cleanup" in group
    assert "'closed'" in group
    assert "'unlabeled'" in group
    cancel = wf["concurrency"]["cancel-in-progress"]
    assert "!= 'closed'" in cancel
    assert "!= 'unlabeled'" in cancel


def test_secret_bearing_jobs_declare_the_environment_and_nothing_else_has_secrets(
    wf: dict[str, Any],
) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for job, spec in wf["jobs"].items():
        body = yaml.safe_dump(spec)
        uses_render = "RENDER_API_KEY" in body or "RENDER_SERVICE_ID" in body
        assert uses_render == (spec.get("environment") == "preview"), job
    assert "secrets: inherit" not in text
    assert "continue-on-error" not in text


def test_write_permissions_are_where_the_design_says(wf: dict[str, Any]) -> None:
    writes = {
        job: {k: v for k, v in (spec.get("permissions") or {}).items() if v == "write"}
        for job, spec in wf["jobs"].items()
    }
    assert writes["publish"] == {"packages": "write"}
    assert writes["report"] == {"pull-requests": "write"}
    assert all(not w for job, w in writes.items() if job not in ("publish", "report"))
    assert wf["jobs"]["report"].get("environment") is None
    assert wf["jobs"]["publish"].get("environment") is None


def test_preview_rechecks_the_pr_before_and_after_deploying(wf: dict[str, Any]) -> None:
    ids = step_ids(wf, "preview")
    assert ids.index("before") < ids.index("deploy") < ids.index("after")
    by_id = {s.get("id"): s for s in steps(wf, "preview")}
    assert "recheck --phase before" in by_id["before"]["run"]
    assert "recheck --phase after" in by_id["after"]["run"]
    assert by_id["deploy"]["if"] == "steps.before.outputs.valid == 'true'"
    smoke = next(s for s in steps(wf, "preview") if "smoke_test.py" in s.get("run", ""))
    assert smoke["if"] == "steps.after.outputs.valid == 'true'"
    assert wf["jobs"]["preview"]["permissions"]["pull-requests"] == "read"
    outputs = wf["jobs"]["preview"]["outputs"]
    assert "superseded" in outputs
    assert "superseded_reason" in outputs


def test_only_trusted_code_runs_in_privileged_jobs(wf: dict[str, Any]) -> None:
    pr_head = "github.event.pull_request.head.sha"
    for job in ("decide", "gate", "publish", "preview", "cleanup", "report"):
        for step in steps(wf, job):
            ref = (step.get("with") or {}).get("ref", "")
            assert pr_head not in ref, (job, step)
    build_checkout = steps(wf, "build-scan")[0]
    assert pr_head in build_checkout["with"]["ref"]
    # the job that builds PR code reads no secret expression at all
    assert "${{ secrets." not in yaml.safe_dump(wf["jobs"]["build-scan"])
    assert "environment" not in wf["jobs"]["build-scan"]


def test_jobs_are_guarded_against_forks_and_dependabot(wf: dict[str, Any]) -> None:
    guard = wf["jobs"]["decide"]["if"]
    assert "head.repo.full_name == github.repository" in guard
    assert "dependabot[bot]" in guard
    for job in ("publish", "preview", "cleanup"):
        assert "head.repo.full_name == github.repository" in wf["jobs"][job]["if"]


def test_cleanup_reports_whether_it_deleted_something(wf: dict[str, Any]) -> None:
    assert wf["jobs"]["cleanup"]["outputs"]["deleted"] == "${{ steps.cleanup.outputs.deleted }}"
    assert "cleanup" in step_ids(wf, "cleanup")
