#!/usr/bin/env python3
# ruff: noqa: E501  (long human-readable messages are clearer unwrapped)
"""Small helpers used by release.yml, rollback.yml and demo-unsafe-deploy.yml.

    release_tools.py gate        --required a,b,c       # NEEDS_JSON env = toJSON(needs)
    release_tools.py preflight                          # HOOK_CONFIGURED, STOCKFLOW_BASE_URL, DEPLOY_REQUIRED (env)
    release_tools.py verify-deployment --release-record F --image REF --expect-sha S --expect-version V --base-url U
    release_tools.py push-digest FILE                   # digest from `docker push` output
    release_tools.py image-ref   --owner O --digest D   # build + validate ghcr.io/O/stockflow@D
    release_tools.py inspect     --image REF            # version + git sha from the image's OCI labels
    release_tools.py observe     --base-url URL        # which version is serving (informational)
    release_tools.py publish-record --image REF --version V --git-sha S --out FILE
    release_tools.py record      --kind K --image REF ...

All untrusted values arrive through environment variables or validated arguments; GitHub context values
are read from the standard GITHUB_* variables, never interpolated into a command line.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import deploy_lib as lib


def _out(key: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target and "\n" not in value:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")
    print(f"{key}={value}")


def _summary(text: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def cmd_gate(args: argparse.Namespace) -> int:
    raw = os.environ.get("NEEDS_JSON", "")
    try:
        needs = json.loads(raw)
    except ValueError:
        print("error: NEEDS_JSON is missing or not JSON", file=sys.stderr)
        return 1
    required = [r for r in args.required.split(",") if r]
    ok, rows = lib.evaluate_needs(needs if isinstance(needs, dict) else {}, required)
    table = ["## Quality gate", "", "| Job | Result |", "|---|---|"]
    table += [
        f"| {name} | {'PASS' if res == 'success' else 'FAIL'}: `{res}` |" for name, res in rows
    ]
    table += ["", f"**{'All required jobs succeeded.' if ok else 'QUALITY GATE FAILED.'}**"]
    _summary("\n".join(table) + "\n")
    for name, res in rows:
        print(f"{name}: {res}")
    if not ok:
        print("quality gate failed: every needed job must be exactly 'success'", file=sys.stderr)
    return 0 if ok else 1


def cmd_preflight(_: argparse.Namespace) -> int:
    """Report what is configured. With DEPLOY_REQUIRED=true an unconfigured target is an ERROR (exit 1)."""
    result = lib.preflight(os.environ)
    _out("configured", str(result["configured"]).lower())
    _out("render_api", str(result["render_api"]).lower())
    _out("base_url", result["base_url"])
    if result["configured"]:
        return 0
    missing = "; ".join(result["missing"])
    if result["required"]:
        print(
            "::error title=Deployment is mandatory but not configured::"
            f"missing {missing}. The image has been published to GHCR but NOT deployed. "
            "Follow docs/SETUP_GITHUB_RENDER.md, then re-run this workflow with workflow_dispatch."
        )
        _summary(
            "## DEPLOYMENT FAILED: not configured (deployment is mandatory)\n\n"
            f"Missing: {missing}.\n\nThe image was built, scanned and published to GHCR, "
            "but **nothing was deployed**. See docs/SETUP_GITHUB_RENDER.md.\n"
        )
        return 1
    print(
        f"::warning title=Live deployment NOT configured::missing {missing}. Nothing will be deployed."
    )
    _summary(
        "## DEPLOYMENT SKIPPED: not configured (require_deploy was false)\n\n"
        f"Missing: {missing}.\n\nThe image was published, but **no deployment happened**.\n"
    )
    return 0


def cmd_verify_deployment(args: argparse.Namespace) -> int:
    """Fail unless release record, hook payload and the serving /api/version all agree."""
    try:
        record = json.loads(Path(args.release_record).read_text(encoding="utf-8"))
        result = json.loads(Path(args.deploy_result).read_text(encoding="utf-8"))
        problems = lib.verify_deployment(
            release_record=record,
            sent_image=str(result.get("image", "")),
            expect_sha=args.expect_sha,
            expect_version=args.expect_version,
            observed=lib.fetch_version(args.base_url),
        )
        if str(result.get("image")) != args.image:
            problems.append("deploy-result.json image differs from the published image of this run")
        if result.get("render_status") not in (None, "live"):
            problems.append(f"render reported {result.get('render_status')!r}, not live")
    except (OSError, ValueError, lib.DeployError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"MISMATCH: {problem}", file=sys.stderr)
    if problems:
        _summary(
            "### Deployment verification FAILED\n\n"
            + "\n".join(f"- {lib.md_safe(p, 300)}" for p in problems)
            + "\n"
        )
        return 1
    print(
        "verified: release record digest == digest sent to Render; /api/version sha and version match"
    )
    return 0


def cmd_push_digest(args: argparse.Namespace) -> int:
    try:
        digest = lib.parse_push_digest(Path(args.file).read_text(encoding="utf-8"))
    except lib.DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _out("digest", digest)
    return 0


def cmd_image_ref(args: argparse.Namespace) -> int:
    try:
        ref = lib.build_image_ref(args.owner, args.digest)
    except lib.DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _out("image", ref)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Read version and git sha from the OCI labels of a published image (never rebuilds anything)."""
    try:
        lib.validate_image_ref(args.image, args.expect_owner)
        completed = subprocess.run(
            [
                *args.docker.split(),
                "buildx",
                "imagetools",
                "inspect",
                args.image,
                "--format",
                "{{json .Image}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if completed.returncode != 0:
            raise lib.DeployError(
                f"image not found or not readable in the registry: {completed.stderr.strip()[:300]}"
            )
        version, sha = lib.release_identity(lib.image_labels(json.loads(completed.stdout)))
    except (lib.DeployError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _out("version", version)
    _out("git_sha", sha)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    result: dict[str, object] = {}
    result_file = Path(args.deploy_result)
    if result_file.is_file():
        result = json.loads(result_file.read_text(encoding="utf-8"))
    configured = args.configured == "true"
    status, deployed, verified = lib.classify_deployment(
        configured=configured,
        deploy_outcome=args.deploy_outcome,
        verify_outcome=args.verify_outcome,
    )
    record = lib.build_record(
        env,
        kind=args.kind,
        image=args.image,
        version=args.version,
        git_sha=args.git_sha,
        tags=[t for t in args.tags.split(",") if t],
        status=status,
        deployed=deployed,
        verified=verified,
        base_url=args.base_url,
        deploy_result=result,
        reason=os.environ.get("DEPLOY_REASON", ""),
        required=args.required == "true",
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    _summary(lib.render_summary(record))
    print(f"deployment record: {status} (deployed={deployed}, verified={verified}) -> {out}")
    if not configured:
        print("::warning title=Nothing was deployed::live deployment is not configured")
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    """Report which version is serving right now (informational; always exits 0)."""
    try:
        data = lib.fetch_version(args.base_url)
    except lib.DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if data is None:
        line = f"{args.label}: the service did not answer /api/version"
    else:
        line = (
            f"{args.label}: serving version {data.get('version')} "
            f"git_sha {data.get('git_sha')} (environment {data.get('environment')})"
        )
    print(line)
    _summary(f"### Observation\n\n{line}\n")
    return 0


def cmd_publish_record(args: argparse.Namespace) -> int:
    try:
        record = lib.build_publish_record(
            dict(os.environ),
            image=args.image,
            version=args.version,
            git_sha=args.git_sha,
            tags=[t for t in args.tags.split(",") if t],
            build_time=args.build_time,
        )
    except lib.DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    _summary(
        "## Image published\n\n"
        f"- image: `{record['image']}`\n- version: `{record['version']}`\n"
        f"- git sha: `{record['git_sha']}`\n- tags: {', '.join(f'`{t}`' for t in record['tags'])}\n"
        f"- rollback: run the rollback workflow with `{record['digest']}`\n"
    )
    print(f"release record -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--required", default="")
    sub.add_parser("preflight")
    ver = sub.add_parser("verify-deployment")
    ver.add_argument("--release-record", required=True)
    ver.add_argument("--deploy-result", default="reports/deploy-result.json")
    ver.add_argument("--image", required=True)
    ver.add_argument("--expect-sha", required=True)
    ver.add_argument("--expect-version", required=True)
    ver.add_argument("--base-url", required=True)
    push = sub.add_parser("push-digest")
    push.add_argument("file")
    ref = sub.add_parser("image-ref")
    ref.add_argument("--owner", required=True)
    ref.add_argument("--digest", required=True)
    insp = sub.add_parser("inspect")
    insp.add_argument("--image", required=True)
    insp.add_argument("--expect-owner")
    insp.add_argument("--docker", default="docker")
    obs = sub.add_parser("observe")
    obs.add_argument("--base-url", required=True)
    obs.add_argument("--label", default="now")
    pub = sub.add_parser("publish-record")
    pub.add_argument("--image", required=True)
    pub.add_argument("--version", required=True)
    pub.add_argument("--git-sha", required=True)
    pub.add_argument("--tags", default="")
    pub.add_argument("--build-time", default="")
    pub.add_argument("--out", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--kind", required=True, choices=["release", "rollback", "demo-unsafe"])
    rec.add_argument("--image", required=True)
    rec.add_argument("--version", required=True)
    rec.add_argument("--git-sha", required=True)
    rec.add_argument("--tags", default="")
    rec.add_argument("--base-url", default="")
    rec.add_argument("--configured", choices=["true", "false"], required=True)
    rec.add_argument("--required", choices=["true", "false"], default="false")
    rec.add_argument("--deploy-outcome", default="skipped")
    rec.add_argument("--verify-outcome", default="skipped")
    rec.add_argument("--deploy-result", default="reports/deploy-result.json")
    rec.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    handlers = {
        "gate": cmd_gate,
        "preflight": cmd_preflight,
        "verify-deployment": cmd_verify_deployment,
        "push-digest": cmd_push_digest,
        "image-ref": cmd_image_ref,
        "inspect": cmd_inspect,
        "record": cmd_record,
        "publish-record": cmd_publish_record,
        "observe": cmd_observe,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
