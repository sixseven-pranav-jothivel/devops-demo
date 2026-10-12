"""Tests for the command-line entry points (deploy_render.py, release_tools.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import deploy_lib as lib
import deploy_render
import pytest
import release_tools

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "ab" * 32
IMAGE = f"ghcr.io/octo-org/stockflow@{DIGEST}"
HOOK = "https://api.render.com/deploy/srv-abc123?key=SECRETKEY99"
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "RENDER_DEPLOY_HOOK_URL",
        "RENDER_API_KEY",
        "RENDER_SERVICE_ID",
        "NEEDS_JSON",
        "HOOK_CONFIGURED",
        "STOCKFLOW_BASE_URL",
        "DEPLOY_REASON",
        "DEPLOY_REQUIRED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/stockflow-demo")
    monkeypatch.setenv("GITHUB_RUN_ID", "7")


def summary(tmp_path: Path) -> str:
    path = tmp_path / "summary.md"
    return path.read_text() if path.exists() else ""


# --------------------------------------------------------------------------- gate
def test_gate_cli_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "NEEDS_JSON", json.dumps({"a": {"result": "success"}, "b": {"result": "success"}})
    )
    assert release_tools.main(["gate", "--required", "a,b"]) == 0
    assert "All required jobs succeeded" in summary(tmp_path)


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_gate_cli_fails_on_bad_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: str
) -> None:
    monkeypatch.setenv(
        "NEEDS_JSON", json.dumps({"a": {"result": "success"}, "b": {"result": result}})
    )
    assert release_tools.main(["gate", "--required", "a,b"]) == 1
    assert "QUALITY GATE FAILED" in summary(tmp_path)


def test_gate_cli_fails_on_missing_required_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEEDS_JSON", json.dumps({"a": {"result": "success"}}))
    assert release_tools.main(["gate", "--required", "a,b"]) == 1


@pytest.mark.parametrize("raw", [None, "", "not json", "[]"])
def test_gate_cli_fails_on_bad_input(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
    if raw is not None:
        monkeypatch.setenv("NEEDS_JSON", raw)
    assert release_tools.main(["gate"]) == 1


# --------------------------------------------------------------------------- preflight
def test_preflight_cli_unconfigured_is_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert release_tools.main(["preflight"]) == 0
    out = capsys.readouterr().out
    assert "::warning title=Live deployment NOT configured" in out
    assert "configured=false" in (tmp_path / "output").read_text()
    assert "DEPLOYMENT SKIPPED" in summary(tmp_path)


def test_preflight_cli_required_fails_and_names_what_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEPLOY_REQUIRED", "true")
    assert release_tools.main(["preflight"]) == 1
    out = capsys.readouterr().out
    assert "::error title=Deployment is mandatory but not configured" in out
    assert "RENDER_DEPLOY_HOOK_URL" in out
    assert "STOCKFLOW_BASE_URL" in out
    assert "docs/SETUP_GITHUB_RENDER.md" in out
    # outputs are still written so later `if: always()` steps (the record) can run
    assert "configured=false" in (tmp_path / "output").read_text()
    assert "DEPLOYMENT FAILED" in summary(tmp_path)


def test_preflight_cli_required_but_configured_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEPLOY_REQUIRED", "true")
    monkeypatch.setenv("HOOK_CONFIGURED", "true")
    monkeypatch.setenv("STOCKFLOW_BASE_URL", "https://stockflow.example")
    assert release_tools.main(["preflight"]) == 0


def test_preflight_cli_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOOK_CONFIGURED", "true")
    monkeypatch.setenv("STOCKFLOW_BASE_URL", "https://stockflow.example")
    assert release_tools.main(["preflight"]) == 0
    assert "configured=true" in (tmp_path / "output").read_text()


# --------------------------------------------------------------------------- digest / ref helpers
def test_push_digest_cli(tmp_path: Path) -> None:
    f = tmp_path / "push.txt"
    f.write_text(f"v: digest: {DIGEST} size: 5\n")
    assert release_tools.main(["push-digest", str(f)]) == 0
    assert f"digest={DIGEST}" in (tmp_path / "output").read_text()
    f.write_text("garbage")
    assert release_tools.main(["push-digest", str(f)]) == 1


def test_image_ref_cli(tmp_path: Path) -> None:
    assert release_tools.main(["image-ref", "--owner", "Octo-Org", "--digest", DIGEST]) == 0
    assert f"image={IMAGE}" in (tmp_path / "output").read_text()
    assert release_tools.main(["image-ref", "--owner", "octo-org", "--digest", "sha256:zz"]) == 2


def test_inspect_cli_rejects_tag_reference() -> None:
    assert release_tools.main(["inspect", "--image", "ghcr.io/octo-org/stockflow:latest"]) == 1


# --------------------------------------------------------------------------- record
def record_args(out: Path, configured: str, deploy: str, verify: str) -> list[str]:
    return [
        "record",
        "--kind",
        "release",
        "--image",
        IMAGE,
        "--version",
        "0.1.0",
        "--git-sha",
        SHA,
        "--tags",
        f"sha-{SHA},0.1.0",
        "--base-url",
        "https://stockflow.example",
        "--configured",
        configured,
        "--deploy-outcome",
        deploy,
        "--verify-outcome",
        verify,
        "--deploy-result",
        str(out.parent / "missing.json"),
        "--out",
        str(out),
    ]


def test_record_cli_unconfigured_says_not_deployed(tmp_path: Path) -> None:
    out = tmp_path / "rec.json"
    assert release_tools.main(record_args(out, "false", "skipped", "skipped")) == 0
    record = json.loads(out.read_text())
    assert record["deployed"] is False
    assert record["verified"] is False
    assert record["deployment"]["status"] == "not_deployed_unconfigured"
    assert "NOT DEPLOYED" in summary(tmp_path)


def test_record_cli_verified(tmp_path: Path) -> None:
    out = tmp_path / "rec.json"
    (tmp_path / "missing.json").write_text(
        json.dumps({"render_deploy_id": "dep-1", "render_status": "live"})
    )
    assert release_tools.main(record_args(out, "true", "success", "success")) == 0
    record = json.loads(out.read_text())
    assert (record["deployed"], record["verified"]) == (True, True)
    assert record["deployment"]["render_deploy_id"] == "dep-1"


# --------------------------------------------------------------------------- deploy_render
def test_deploy_render_rejects_tag_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    assert deploy_render.main(["--image", "ghcr.io/octo-org/stockflow:latest"]) == 2


def test_deploy_render_rejects_wrong_owner() -> None:
    assert deploy_render.main(["--image", IMAGE, "--expect-owner", "someone", "--dry-run"]) == 2


def test_deploy_render_requires_hook() -> None:
    assert deploy_render.main(["--image", IMAGE]) == 2


def test_deploy_render_dry_run_never_prints_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    assert deploy_render.main(["--image", IMAGE, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert (
        "::add-mask::" in out
    )  # the runner is told to mask it (the mask command itself carries the value)
    shown = "\n".join(line for line in out.splitlines() if not line.startswith("::add-mask::"))
    assert "SECRETKEY99" not in shown


def test_deploy_render_http_only_hook_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", "http://api.render.com/deploy/srv-x?key=k")
    assert deploy_render.main(["--image", IMAGE]) == 2


def test_deploy_render_success_writes_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    monkeypatch.setattr(
        lib, "trigger_hook", lambda hook, image: lib.HookResult(200, "dep-77", False)
    )
    result = tmp_path / "res.json"
    assert deploy_render.main(["--image", IMAGE, "--result-file", str(result)]) == 0
    data = json.loads(result.read_text())
    assert data["hook_http_status"] == 200
    assert data["render_deploy_id"] == "dep-77"
    assert "render_deploy_id=dep-77" in (tmp_path / "output").read_text()


def test_deploy_render_failure_never_leaks_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)

    def boom(hook: str, image: str) -> lib.HookResult:
        raise lib.DeployError(f"broken talking to {hook}")

    monkeypatch.setattr(lib, "trigger_hook", boom)
    assert deploy_render.main(["--image", IMAGE, "--result-file", str(tmp_path / "r.json")]) == 1
    captured = capsys.readouterr()
    shown = "\n".join(
        line for line in captured.out.splitlines() if not line.startswith("::add-mask::")
    )
    assert "SECRETKEY99" not in shown + captured.err


def test_deploy_render_polls_render_api_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    monkeypatch.setenv("RENDER_API_KEY", "apikey-123456")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-abc123")

    class FakeApi:
        def __init__(self, key: str, service: str) -> None:
            pass

        def list_deploys(self) -> list[lib.RenderDeploy]:
            return [lib.RenderDeploy("dep-old", "live", None, "sha256:" + "cd" * 32)]

        def live_image_sha(self) -> str:
            return "sha256:" + "cd" * 32

    monkeypatch.setattr(lib, "RenderApi", FakeApi)
    monkeypatch.setattr(
        lib, "trigger_hook", lambda hook, image: lib.HookResult(200, "dep-77", False)
    )
    monkeypatch.setattr(
        lib,
        "wait_for_render_deploy",
        lambda api, **kw: lib.RenderOutcome("dep-77", "update_failed", False),
    )
    result = tmp_path / "res.json"
    assert deploy_render.main(["--image", IMAGE, "--result-file", str(result)]) == 1
    data = json.loads(result.read_text())
    assert data["render_status"] == "update_failed"
    assert data["previous_image_sha"] == "sha256:" + "cd" * 32


# ---- inspect / publish-record / observe
def fake_docker(tmp_path: Path, payload: str, code: int = 0) -> str:
    script = tmp_path / "fake_docker.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({payload!r})\n"
        f"sys.stderr.write('not found' if {code} else '')\n"
        f"sys.exit({code})\n"
    )
    return f"{sys.executable} {script}"


def test_inspect_cli_reads_labels(tmp_path: Path) -> None:
    labels = {"org.opencontainers.image.version": "0.1.0", "org.opencontainers.image.revision": SHA}
    docker = fake_docker(tmp_path, json.dumps({"config": {"Labels": labels}}))
    assert release_tools.main(["inspect", "--image", IMAGE, "--docker", docker]) == 0
    out = (tmp_path / "output").read_text()
    assert "version=0.1.0" in out
    assert f"git_sha={SHA}" in out


def test_inspect_cli_missing_image_fails(tmp_path: Path) -> None:
    assert (
        release_tools.main(["inspect", "--image", IMAGE, "--docker", fake_docker(tmp_path, "", 1)])
        == 1
    )


def test_inspect_cli_unlabelled_image_fails(tmp_path: Path) -> None:
    docker = fake_docker(tmp_path, json.dumps({"config": {"Labels": {}}}))
    assert release_tools.main(["inspect", "--image", IMAGE, "--docker", docker]) == 1


def test_publish_record_cli(tmp_path: Path) -> None:
    out = tmp_path / "release-record.json"
    args = ["publish-record", "--image", IMAGE, "--version", "0.1.0", "--git-sha", SHA]
    assert release_tools.main([*args, "--tags", f"sha-{SHA},0.1.0", "--out", str(out)]) == 0
    record = json.loads(out.read_text())
    assert record["digest"] == DIGEST
    assert record["tags"] == [f"sha-{SHA}", "0.1.0"]
    assert release_tools.main([*args[:-1], "short", "--out", str(out)]) == 1


def test_observe_cli_is_informational(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        lib,
        "fetch_version",
        lambda url: {"version": "0.1.0", "git_sha": SHA, "environment": "production"},
    )
    assert (
        release_tools.main(["observe", "--base-url", "https://x.example", "--label", "after"]) == 0
    )
    assert SHA in capsys.readouterr().out
    monkeypatch.setattr(lib, "fetch_version", lambda url: None)
    assert release_tools.main(["observe", "--base-url", "https://x.example"]) == 0
    monkeypatch.undo()
    assert release_tools.main(["observe", "--base-url", "ftp://x"]) == 2


# ---- verify-deployment -----------------------------------------------------------
def write_inputs(
    tmp_path: Path, record_digest: str = DIGEST, sent: str = IMAGE
) -> tuple[Path, Path]:
    record = tmp_path / "release-record.json"
    record.write_text(
        json.dumps({"digest": record_digest, "image": IMAGE, "git_sha": SHA, "version": "0.1.0"})
    )
    result = tmp_path / "deploy-result.json"
    result.write_text(json.dumps({"image": sent, "render_status": "live"}))
    return record, result


def verify_args(record: Path, result: Path) -> list[str]:
    return [
        "verify-deployment",
        "--release-record",
        str(record),
        "--deploy-result",
        str(result),
        "--image",
        IMAGE,
        "--expect-sha",
        SHA,
        "--expect-version",
        "0.1.0",
        "--base-url",
        "https://x.example",
    ]


def test_verify_deployment_cli_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    record, result = write_inputs(tmp_path)
    serving = {"name": "stockflow", "git_sha": SHA, "version": "0.1.0"}
    monkeypatch.setattr(lib, "fetch_version", lambda url: serving)
    assert release_tools.main(verify_args(record, result)) == 0


def test_verify_deployment_cli_fails_on_wrong_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record, result = write_inputs(tmp_path)
    serving = {"name": "stockflow", "git_sha": "f" * 40, "version": "0.1.0"}
    monkeypatch.setattr(lib, "fetch_version", lambda url: serving)
    assert release_tools.main(verify_args(record, result)) == 1
    assert "deployed git_sha" in capsys.readouterr().err
    assert "verification FAILED" in summary(tmp_path)


def test_verify_deployment_cli_fails_when_sent_digest_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = "ghcr.io/octo-org/stockflow@sha256:" + "cd" * 32
    record, result = write_inputs(tmp_path, sent=other)
    serving = {"name": "stockflow", "git_sha": SHA, "version": "0.1.0"}
    monkeypatch.setattr(lib, "fetch_version", lambda url: serving)
    assert release_tools.main(verify_args(record, result)) == 1


def test_verify_deployment_cli_fails_when_render_not_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record, result = write_inputs(tmp_path)
    result.write_text(json.dumps({"image": IMAGE, "render_status": "update_failed"}))
    serving = {"name": "stockflow", "git_sha": SHA, "version": "0.1.0"}
    monkeypatch.setattr(lib, "fetch_version", lambda url: serving)
    assert release_tools.main(verify_args(record, result)) == 1


def test_verify_deployment_cli_fails_on_missing_files(tmp_path: Path) -> None:
    assert release_tools.main(verify_args(tmp_path / "nope.json", tmp_path / "nope2.json")) == 1


def test_record_cli_required_flag_is_recorded(tmp_path: Path) -> None:
    out = tmp_path / "rec.json"
    argv = [*record_args(out, "false", "skipped", "skipped"), "--required", "true"]
    assert release_tools.main(argv) == 0
    record = json.loads(out.read_text())
    assert record["deploy_required"] is True
    assert record["deployed"] is False
    assert "mandatory" in summary(tmp_path)
