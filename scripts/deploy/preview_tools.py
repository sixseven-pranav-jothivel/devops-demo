#!/usr/bin/env python3
# ruff: noqa: E501  (long human-readable messages are clearer unwrapped)
"""Commands for .github/workflows/pr-preview.yml. Untrusted values arrive only through environment variables.

    preview_tools.py decide                      # pull_request event in env -> deploy | cleanup | skip
    preview_tools.py wait-gate  --timeout S      # wait for the quality-gate check run of PR_HEAD_SHA
    preview_tools.py preflight                   # is the Render API configured? (fails loudly if not)
    preview_tools.py deploy  --image REF         # create/redeploy the PR's preview by digest, wait until live
    preview_tools.py recheck --phase before|after   # PR still open, labelled, same head? (else skip/delete)
    preview_tools.py cleanup                     # delete the PR's preview (a missing one is fine)
    preview_tools.py comment                     # create/update the single sticky PR comment

Secrets (RENDER_API_KEY, RENDER_SERVICE_ID) are read from the environment of the `deploy` and `cleanup`
steps only. The scripts of the TRUSTED base commit run these commands; PR code is never executed here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import deploy_lib as lib
import preview_lib as pv


def _out(key: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target and "\n" not in value and "\r" not in value:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")
    print(f"{key}={value}")


def _summary(text: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def _api(*, need_key: bool = True) -> pv.PreviewApi:
    key = os.environ.get("RENDER_API_KEY", "").strip()
    service = os.environ.get("RENDER_SERVICE_ID", "").strip()
    if need_key and not (key and service):
        raise lib.DeployError("RENDER_API_KEY / RENDER_SERVICE_ID are not set")
    print(*lib.github_mask_commands([key]), sep="\n")
    return pv.PreviewApi(key, service)


def _guard(expected: str) -> pv.Decision:
    """Second, script-level enforcement of the same rules as the job-level ``if``."""
    decision = pv.decide(os.environ)
    if decision.action != expected:
        raise lib.DeployError(
            f"guard: this event is '{decision.action}' ({decision.reason}), not '{expected}'"
        )
    return decision


def cmd_decide(_: argparse.Namespace) -> int:
    decision = pv.decide(os.environ)
    _out("action", decision.action)
    _out("reason", lib.md_safe(decision.reason))
    _out("pr_number", str(decision.pr_number))
    _out("head_sha", decision.head_sha)
    print(f"preview decision: {decision.action} ({decision.reason})")
    return 0


def cmd_wait_gate(args: argparse.Namespace) -> int:
    decision = _guard("deploy")
    github = pv.GitHubApi(
        os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    )
    state = pv.wait_for_gate(
        lambda: github.check_runs(decision.head_sha), timeout=args.timeout, interval=args.interval
    )
    _out("gate", state)
    if state != "success":
        print(
            f"::error title=No preview::quality-gate is '{state}' for {decision.head_sha[:7]}; "
            "a red or unfinished pull request gets no preview"
        )
        return 1
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    missing = [
        name
        for name, present in (
            ("secret RENDER_API_KEY", os.environ.get("RENDER_KEY_PRESENT") == "true"),
            ("secret RENDER_SERVICE_ID", os.environ.get("RENDER_SERVICE_PRESENT") == "true"),
        )
        if not present
    ]
    _out("configured", "false" if missing else "true")
    if not missing:
        return 0
    detail = ", ".join(missing)
    if (
        args.optional
    ):  # cleanup: without credentials no preview can exist, so there is nothing to delete
        print(f"::warning title=Preview not configured::missing {detail}; nothing to clean up")
        return 0
    print(
        f"::error title=NOT DEPLOYED: preview not configured::missing {detail} in the `preview` environment. "
        "Nothing was deployed. See docs/SETUP_GITHUB_RENDER.md."
    )
    _summary(
        "## NOT DEPLOYED: preview not configured\n\n"
        f"Missing: {detail}. The image was published but no preview exists. "
        "See docs/SETUP_GITHUB_RENDER.md.\n"
    )
    return 1


def cmd_deploy(args: argparse.Namespace) -> int:
    try:
        decision = _guard("deploy")
        lib.validate_image_ref(args.image, args.expect_owner)
        api = _api()
        plan = os.environ.get("PREVIEW_PLAN", "").strip() or None
        result = pv.deploy_preview(
            api,
            key=os.environ["RENDER_API_KEY"].strip(),
            image_ref=args.image,
            pr_number=decision.pr_number,
            plan=plan,
            timeout=args.timeout,
            interval=args.interval,
        )
    except (lib.DeployError, KeyError) as exc:
        print(f"::error title=Preview deploy failed::{lib.md_safe(exc, 300)}", file=sys.stderr)
        return 1
    record: dict[str, Any] = {
        "pr": decision.pr_number,
        "service_id": result.service_id,
        "deploy_id": result.deploy_id,
        "url": result.url,
        "image": args.image,
        "created": result.created,
    }
    Path(args.result_file).parent.mkdir(parents=True, exist_ok=True)
    Path(args.result_file).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    _out("url", result.url)
    _out("service_id", result.service_id)
    _out("deploy_id", result.deploy_id or "")
    return 0


def cmd_recheck(args: argparse.Namespace) -> int:
    """Is the PR still open, labelled and at the built commit? Run before and after the preview is deployed.

    Closed or unlabeled => no preview may exist: in the ``after`` phase it is deleted here. A changed head
    commit only supersedes this run (the newer run owns the preview). Not being able to ask is an error.
    """
    try:
        decision = _guard("deploy")
        github = pv.GitHubApi(
            os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
        )
        code, reason = pv.pr_state(github.pull(decision.pr_number), decision.head_sha)
        _out("valid", "true" if code == "ok" else "false")
        _out("code", code)
        _out("reason", lib.md_safe(reason))
        if code == "ok":
            return 0
        print(f"::warning title=Preview superseded ({args.phase})::{lib.md_safe(reason)}")
        deleted = False
        if args.phase == "after" and code in ("closed", "unlabeled"):
            api = _api()
            found = api.find(pv.preview_name(decision.pr_number))
            deleted = found is not None and api.delete(found.service_id)
        _out("deleted", str(deleted).lower())
        _summary(
            f"## Preview superseded ({args.phase} deploy)\n\n{lib.md_safe(reason)}\n"
            + ("The preview that had just been created was deleted.\n" if deleted else "")
        )
    except lib.DeployError as exc:
        print(
            f"::error title=Could not re-check the pull request::{lib.md_safe(exc, 300)}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_cleanup(_: argparse.Namespace) -> int:
    try:
        decision = _guard("cleanup")
        api = _api()
        name = pv.preview_name(decision.pr_number)
        found = api.find(name)
        if found is None:
            print(f"no preview service named {name}; nothing to delete")
            _out("deleted", "false")
            return 0
        deleted = api.delete(found.service_id)
        print(f"preview service {name} {'deleted' if deleted else 'was already gone'}")
        _out("deleted", str(deleted).lower())
    except lib.DeployError as exc:
        print(f"::error title=Preview cleanup failed::{lib.md_safe(exc, 300)}", file=sys.stderr)
        return 1
    return 0


def cmd_comment(_: argparse.Namespace) -> int:
    try:
        decision = pv.decide(os.environ)
        if decision.action not in ("deploy", "cleanup"):
            print(f"no comment: {decision.reason}")
            return 0
        needs = json.loads(os.environ.get("NEEDS_JSON", "{}"))
        state = pv.derive_state(needs if isinstance(needs, dict) else {}, decision.action)
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        repo, run_id = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", "")
        run_url = f"{server}/{repo}/actions/runs/{run_id}" if run_id else ""
        body = pv.render_comment(
            state, pr_number=decision.pr_number, head_sha=decision.head_sha, run_url=run_url
        )
        github = pv.GitHubApi(os.environ.get("GITHUB_TOKEN", ""), repo)
        if (
            state["status"] == "nothing_to_delete"
            and github.find_comment(decision.pr_number) is None
        ):
            print("no preview ever existed for this pull request: not adding a comment")
            return 0
        verb = github.upsert_comment(decision.pr_number, body)
    except (lib.DeployError, ValueError) as exc:
        print(
            f"::error title=Could not update the PR comment::{lib.md_safe(exc, 300)}",
            file=sys.stderr,
        )
        return 1
    _summary(body.replace(pv.COMMENT_MARKER, "").lstrip())
    print(f"sticky comment {verb}: {state['headline']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("decide")
    gate = sub.add_parser("wait-gate")
    gate.add_argument("--timeout", type=float, default=1500.0)
    gate.add_argument("--interval", type=float, default=20.0)
    pre = sub.add_parser("preflight")
    pre.add_argument("--optional", action="store_true", help="warn instead of failing (cleanup)")
    dep = sub.add_parser("deploy")
    dep.add_argument("--image", required=True)
    dep.add_argument("--expect-owner")
    dep.add_argument("--timeout", type=float, default=900.0)
    dep.add_argument("--interval", type=float, default=10.0)
    dep.add_argument("--result-file", default="reports/preview-result.json")
    rec = sub.add_parser("recheck")
    rec.add_argument("--phase", choices=["before", "after"], required=True)
    sub.add_parser("cleanup")
    sub.add_parser("comment")
    args = parser.parse_args(argv)
    handlers = {
        "decide": cmd_decide,
        "wait-gate": cmd_wait_gate,
        "preflight": cmd_preflight,
        "deploy": cmd_deploy,
        "recheck": cmd_recheck,
        "cleanup": cmd_cleanup,
        "comment": cmd_comment,
    }
    try:
        return handlers[args.cmd](args)
    except lib.DeployError as exc:
        print(f"::error::{lib.md_safe(exc, 300)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
