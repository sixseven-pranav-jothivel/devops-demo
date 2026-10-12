#!/usr/bin/env python3
# ruff: noqa: E501  (long human-readable messages are clearer unwrapped)
"""Deploy an exact image digest to the image-backed Render service through its deploy hook.

    RENDER_DEPLOY_HOOK_URL=... python scripts/deploy/deploy_render.py \
        --image ghcr.io/<owner>/stockflow@sha256:<64hex> [--expect-owner <owner>] [--dry-run]

Environment (read here, never from the command line, never printed):
    RENDER_DEPLOY_HOOK_URL   required unless --dry-run; contains the hook key
    RENDER_API_KEY           optional; with RENDER_SERVICE_ID the script also polls the Render API
    RENDER_SERVICE_ID        optional (srv-...)

A 200 from the hook only means "accepted". With the API credentials the script waits until the deploy is
``live`` (or failed/canceled); without them the caller must verify through /api/version and the smoke test.
The outcome is written to ``--result-file`` (JSON) and, in GitHub Actions, to ``$GITHUB_OUTPUT``.

Exit status: 0 hook accepted (and, when polled, deploy live), 1 deploy failed, 2 usage/configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import deploy_lib as lib


def _write_output(key: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target and "\n" not in value:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--image", required=True, help="ghcr.io/<owner>/stockflow@sha256:<64hex>")
    parser.add_argument("--expect-owner", help="registry owner the image must belong to")
    parser.add_argument("--dry-run", action="store_true", help="validate and print, send nothing")
    parser.add_argument("--result-file", type=Path, default=Path("reports/deploy-result.json"))
    parser.add_argument("--wait-timeout", type=float, default=600.0)
    parser.add_argument("--wait-interval", type=float, default=10.0)
    return parser.parse_args(argv)


def _open_api(result: dict[str, Any]) -> tuple[lib.RenderApi | None, list[str]]:
    """Optional Render API client plus the ids of deploys that exist before we trigger ours."""
    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    service_id = os.environ.get("RENDER_SERVICE_ID", "").strip()
    if not (api_key and service_id):
        print(
            "render api: RENDER_API_KEY/RENDER_SERVICE_ID not set; "
            "deploy status will be inferred from /api/version"
        )
        return None, []
    try:
        api = lib.RenderApi(api_key, service_id)
        known = [d.deploy_id for d in api.list_deploys()]
        result["previous_image_sha"] = api.live_image_sha()
    except lib.DeployError as exc:
        print(f"warning: render api unavailable before deploy ({exc}); continuing without it")
        return None, []
    print(f"render api: previous live image sha: {result['previous_image_sha'] or 'unknown'}")
    return api, known


def _deploy(
    args: argparse.Namespace, hook: str, digest: str, result: dict[str, Any], secrets: list[str]
) -> int:
    api, known_ids = _open_api(result)
    try:
        hook_result = lib.trigger_hook(hook, args.image)
        result["hook_http_status"] = hook_result.status
        result["render_deploy_id"] = hook_result.deploy_id
        print(
            f"deploy hook accepted the request (HTTP {hook_result.status}"
            f"{', queued behind a running deploy' if hook_result.queued else ''})."
            " Accepted is not deployed: waiting for evidence."
        )
        if api is None:
            return 0
        outcome = lib.wait_for_render_deploy(
            api,
            digest=digest,
            deploy_id=hook_result.deploy_id,
            known_ids=known_ids,
            timeout=args.wait_timeout,
            interval=args.wait_interval,
        )
    except lib.DeployError as exc:
        print(f"error: {lib.redact(str(exc), secrets)}", file=sys.stderr)
        return 1
    result["render_deploy_id"] = outcome.deploy_id
    result["render_status"] = outcome.status
    print(f"render deploy result: {outcome.status}")
    return 0 if outcome.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        _, digest = lib.validate_image_ref(args.image, args.expect_owner)
    except lib.DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    hook = os.environ.get("RENDER_DEPLOY_HOOK_URL", "").strip()
    if args.dry_run:
        print(f"dry run: would deploy {args.image}")
        if hook:
            print(*lib.github_mask_commands([hook]), sep="\n")
            shown = lib.redact(lib.build_hook_url(hook, args.image), [hook])
            print(f"dry run: hook url (redacted): {shown}")
        return 0
    if not hook:
        print("error: RENDER_DEPLOY_HOOK_URL is not set", file=sys.stderr)
        return 2
    try:
        secrets = lib.hook_secrets(hook, args.image)
    except lib.DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(*lib.github_mask_commands(secrets), sep="\n")  # runner masks the hook and derived URL

    result: dict[str, Any] = {
        "image": args.image,
        "hook_http_status": None,
        "render_deploy_id": None,
        "render_status": None,
        "previous_image_sha": None,
    }
    exit_code = _deploy(args, hook, digest, result, secrets)
    args.result_file.parent.mkdir(parents=True, exist_ok=True)
    args.result_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    _write_output("render_deploy_id", str(result["render_deploy_id"] or ""))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
