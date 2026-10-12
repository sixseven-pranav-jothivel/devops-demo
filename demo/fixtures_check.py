"""``python demo/inject.py --validate-fixtures``: prove fixtures still trigger the real scanners.

The advisories behind bad_dependency / bad_container live in external databases and can change
(withdrawn, re-scored, a base image tag removed). This check fails LOUDLY, naming the scenario, if a
fixture no longer produces its documented finding, instead of letting a demo silently pass its gate.
It also dry-runs every scenario patch against the current baseline (no writes in the live tree).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from demo.engine import EXIT_ERROR, EXIT_OK, EXIT_UNVERIFIED, DemoError, Engine
from demo.verify import network_available, tool_env

TIMEOUT = 600


class UnverifiedError(Exception):
    """The check could not run (offline, tool missing); NOT a pass."""


def _run(argv: list[str], cwd: Path, engine: Engine) -> subprocess.CompletedProcess[str]:
    env = tool_env(engine.root, sandbox=False)
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )


def _pip_audit_ids(engine: Engine, requirements: Path) -> set[str]:
    proc = _run(
        [
            "uv",
            "run",
            "pip-audit",
            "-r",
            str(requirements),
            "--no-deps",
            "--disable-pip",
            "-f",
            "json",
        ],
        engine.root,
        engine,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise UnverifiedError(
            f"pip-audit gave no JSON (offline?): {proc.stderr.strip()[-300:]}"
        ) from exc
    ids: set[str] = set()
    for dep in data.get("dependencies", []):
        if dep.get("skip_reason"):
            raise UnverifiedError(f"pip-audit skipped {dep.get('name')}: {dep['skip_reason']}")
        for vuln in dep.get("vulns", []):
            ids.add(vuln["id"])
            ids.update(vuln.get("aliases", []))
    return ids


def _trivy_ids(args: list[str], engine: Engine) -> tuple[set[str], int]:
    proc = _run(["trivy", *args, "--format", "json", "--quiet"], engine.root, engine)
    try:
        data = json.loads(proc.stdout[proc.stdout.index("{") :])
    except ValueError as exc:  # JSONDecodeError is a ValueError
        raise UnverifiedError(
            f"trivy gave no JSON: {(proc.stderr or proc.stdout).strip()[-300:]}"
        ) from exc
    ids: set[str] = set()
    count = 0
    for result in data.get("Results", []):
        for vuln in result.get("Vulnerabilities") or []:
            ids.add(vuln["VulnerabilityID"])
            count += 1
    return ids, count


def check_pip_audit(engine: Engine, check: dict[str, Any]) -> str:
    wanted = set(check["expect_ids"])
    found = _pip_audit_ids(engine, engine.root / check["requirements"])
    gone = wanted - found
    if gone:
        raise DemoError(
            f"STALE FIXTURE ({check['scenario']}): pip-audit no longer reports {sorted(gone)} for "
            f"{check['requirements']}. The scenario would no longer fail the dependency gate. Pick "
            "another vulnerable pin and update scenarios.yaml (see demo/README.md)."
        )
    return f"pip-audit reports {sorted(wanted)} ({len(found)} advisory ids total)"


def check_trivy_fs(engine: Engine, check: dict[str, Any]) -> str:
    with tempfile.TemporaryDirectory(prefix="demo-trivy-fs-") as tmp:
        shutil.copy(engine.root / check["requirements"], Path(tmp) / "requirements.txt")
        found, count = _trivy_ids(
            ["fs", "--scanners", "vuln", "--severity", "HIGH,CRITICAL", tmp], engine
        )
    gone = set(check["expect_ids"]) - found
    if gone:
        raise DemoError(
            f"STALE FIXTURE ({check['scenario']}): trivy fs no longer reports {sorted(gone)} at "
            f"HIGH/CRITICAL for {check['requirements']} (found {sorted(found)})."
        )
    return f"trivy fs reports {sorted(check['expect_ids'])} at HIGH/CRITICAL ({count} findings)"


def check_trivy_image_ref(engine: Engine, check: dict[str, Any]) -> str:
    _, count = _trivy_ids(
        [
            "image",
            "--image-src",
            "remote",
            "--platform",
            "linux/amd64",
            "--scanners",
            "vuln",
            "--severity",
            "HIGH,CRITICAL",
            "--ignore-unfixed",
            check["image"],
        ],
        engine,
    )
    if count < int(check["min_findings"]):
        raise DemoError(
            f"STALE FIXTURE ({check['scenario']}): {check['image']} now has only {count} fixable "
            f"HIGH/CRITICAL findings (need >= {check['min_findings']})."
        )
    return f"{check['image'].split('@')[0]} has {count} fixable HIGH/CRITICAL findings"


def check_lock_consistency(engine: Engine, check: dict[str, Any]) -> str:
    scenario = engine.config.get(check["scenario"])
    with tempfile.TemporaryDirectory(prefix="demo-lock-") as tmp:
        work = Path(tmp)
        for name in ("pyproject.toml", "uv.lock", ".python-version", "README.md"):
            if (engine.root / name).exists():
                shutil.copy(engine.root / name, work / name)
        patched = Engine(engine.root, engine.root / "demo" / "scenarios.yaml")
        for rel, data in patched.compute(scenario).items():
            (work / rel).write_bytes(data)
        proc = _run(["uv", "lock", "--check"], work, engine)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-400:]
            if any(word in tail.lower() for word in ("network", "dns", "connect")):
                raise UnverifiedError(f"uv lock --check could not reach the index: {tail}")
            raise DemoError(
                f"STALE FIXTURE ({scenario.id}): patched uv.lock is inconsistent:\n{tail}"
            )
    return "patched pyproject.toml + uv.lock pass `uv lock --check`"


CHECKS = {
    "pip-audit": check_pip_audit,
    "trivy-fs": check_trivy_fs,
    "trivy-image-ref": check_trivy_image_ref,
    "lock-consistency": check_lock_consistency,
}
NETWORK_CHECKS = {"pip-audit", "trivy-image-ref", "lock-consistency", "trivy-fs"}


def check_patches_apply(engine: Engine) -> list[tuple[str, str, str]]:
    """Dry-run every scenario patch against the current tree (pure; nothing is written)."""
    rows: list[tuple[str, str, str]] = []
    for scenario in engine.config.scenarios:
        if not scenario.patch:
            continue
        try:
            engine.compute(scenario)
            rows.append((scenario.id, "OK", "patch applies cleanly to the current baseline"))
        except DemoError as exc:
            status = "UNVERIFIED" if "does not exist" in str(exc) else "FAIL"
            rows.append((scenario.id, status, str(exc)))
    return rows


def run(engine: Engine, *, offline: bool) -> int:
    failures = 0
    unverified = 0
    print("[demo] validating fixtures against the real tools ...")
    for scenario_id, status, detail in check_patches_apply(engine):
        print(f"[demo]   patch    {scenario_id:<22} {status}: {detail}")
        failures += status == "FAIL"
        unverified += status == "UNVERIFIED"
    if offline or not network_available():
        offline = True
        print("[demo]   WARNING: offline; network-backed advisory checks were NOT run")
    for check in engine.config.fixture_checks:
        kind = check["kind"]
        label = f"{kind:<16} {check['scenario']}"
        if offline and kind in NETWORK_CHECKS:
            print(f"[demo]   {label} SKIPPED (offline)")
            unverified += 1
            continue
        try:
            print(f"[demo]   {label} OK: {CHECKS[kind](engine, check)}")
        except UnverifiedError as exc:
            print(f"[demo]   {label} UNVERIFIED: {exc}")
            unverified += 1
        except DemoError as exc:
            print(f"[demo]   {label} FAIL: {exc}")
            failures += 1
    if failures:
        print(f"[demo] {failures} fixture check(s) FAILED. Do not demo the affected scenarios.")
        return EXIT_ERROR
    if unverified:
        print(f"[demo] {unverified} check(s) could not be verified (not a pass).")
        return EXIT_OK if offline else EXIT_UNVERIFIED
    print("[demo] all fixtures valid.")
    return EXIT_OK
