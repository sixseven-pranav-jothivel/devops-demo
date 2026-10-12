"""Command line front end: ``python -m demo <command>`` (also demo/inject.py, demo/reset.py)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from demo import fixtures_check, verify
from demo.engine import EXIT_OK, DemoError, Engine, Report, Scenario

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m demo", description="StockFlow reversible defect injection."
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list scenarios")

    show = sub.add_parser("show", help="describe one scenario (files, gate, repro, remediation)")
    show.add_argument("scenario")

    inject = sub.add_parser("inject", help="apply a scenario (switching restores baseline first)")
    inject.add_argument("scenario")
    inject.add_argument("--force", action="store_true", help="override edit/existing-file refusals")
    inject.add_argument(
        "--allow-runtime-failures",
        action="store_true",
        help="acknowledge a runtime-failure scenario (demo.allow_runtime_failures is false)",
    )

    reset = sub.add_parser("reset", help="restore the exact clean files")
    reset.add_argument("--force", action="store_true", help="discard user edits (copies are kept)")

    sub.add_parser("status", help="show the active scenario and file states")

    verify = sub.add_parser("verify", help="inject, assert the real gate fails, reset, assert pass")
    verify.add_argument("scenario")
    verify.add_argument("--json", type=Path, help="also write results to this JSON file")
    verify.add_argument("--in-place", action="store_true", help="use the live tree, not a copy")

    verify_all = sub.add_parser("verify-all", help="verify every scenario and print a table")
    verify_all.add_argument("--json", type=Path, help="also write results to this JSON file")
    verify_all.add_argument("--strict", action="store_true", help="treat SKIPPED as failure")
    verify_all.add_argument("--in-place", action="store_true", help="use the live tree, not a copy")

    fixtures = sub.add_parser("validate-fixtures", help="re-check fixtures against real tools")
    fixtures.add_argument("--offline", action="store_true", help="skip network-backed checks")

    guard = sub.add_parser("image-guard", help="assert a built image carries no demo material")
    guard.add_argument("image")
    guard.add_argument("--app-dir", default="/app")
    return parser


def _print_report(report: Report) -> None:
    label = report.scenario or "-"
    for message in report.messages:
        print(f"[demo] {report.action} {label}: {message}")
    for rel in report.changed:
        print(f"[demo]   {rel}")


def _print_list(engine: Engine) -> None:
    rows = [("ID", "CATEGORY", "GATE", "STAGE", "DEPLOY?", "NAME")]
    for s in engine.config.scenarios:
        rows.append(
            (
                s.id,
                s.category,
                str(s.expected["gate"]),
                str(s.expected["stage"]),
                "yes" if s.deployment_allowed else "no",
                s.name,
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


def _print_show(s: Scenario) -> None:
    expected = s.expected
    print(f"{s.id}: {s.name}\n  category: {s.category}\n  {s.description}")
    print(f"  deployment_allowed: {s.deployment_allowed}")
    print(f"  files: {', '.join(s.files) or '(none)'}")
    print(f"  expected: stage={expected['stage']} gate={expected['gate']}")
    print(f"  command: {expected['command']}")
    print(f"  signature: {expected['output_contains']}")
    if s.runtime_env:
        env = " ".join(f"{k}={v}" for k, v in s.runtime_env.items())
        print(f"  runtime env (both required): {env}")
    print("  reproduce:")
    for command in s.reproduce:
        print(f"    $ {command}")
    print(f"  remediation: {s.remediation}")


def _print_status(engine: Engine) -> None:
    status = engine.status()
    if status["active"] is None:
        print("[demo] baseline: no scenario active")
        for rel in status.get("stray_markers", []):
            print(f"[demo] WARNING: {rel} contains DEMO-DEFECT markers but no state exists")
        return
    print(f"[demo] active scenario: {status['active']} ({status['status']})")
    for rel, state in sorted(status["files"].items()):
        print(f"[demo]   {rel}: {state}")


def run(args: argparse.Namespace) -> int:
    engine = Engine(args.root)
    command = args.command
    if command == "list":
        _print_list(engine)
    elif command == "show":
        _print_show(engine.config.get(args.scenario))
    elif command == "inject":
        _print_report(
            engine.inject(
                args.scenario, force=args.force, allow_runtime=args.allow_runtime_failures
            )
        )
        if engine.config.get(args.scenario).runtime:
            print("[demo] runtime fault is INERT until the process sets its env keys; see 'show'.")
    elif command == "reset":
        _print_report(engine.reset(force=args.force))
    elif command == "status":
        _print_status(engine)
    elif command == "verify":
        return verify.run_verify(
            engine, [args.scenario], args.json, strict=True, in_place=args.in_place
        )
    elif command == "verify-all":
        ids = [s.id for s in engine.config.scenarios]
        return verify.run_verify(engine, ids, args.json, strict=args.strict, in_place=args.in_place)
    elif command == "validate-fixtures":
        return fixtures_check.run(engine, offline=args.offline)
    elif command == "image-guard":
        return verify.image_guard(args.image, args.app_dir)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except DemoError as exc:
        print(f"[demo] ERROR: {exc}", file=sys.stderr)
        return exc.code
