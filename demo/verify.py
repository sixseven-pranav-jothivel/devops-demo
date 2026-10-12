"""Scenario verification: inject -> real gate must FAIL as declared -> reset -> gate must PASS.

``python demo/verify.py [ID ...]`` (or ``python -m demo verify|verify-all``). By default the work
is done in a throw-away COPY of the repository (``--in-place`` uses the live tree), so concurrent
work on the real tree never sees an injected defect. The copy reuses the live ``.venv``
(``UV_NO_SYNC=1``), so nothing is installed or modified, even for ``bad_dependency``.

Nothing is faked: each row is the exit code and output of the real tool (ruff, mypy, pytest,
coverage, bandit, pip-audit, trivy, docker build, k6, the smoke test, the app process itself).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.engine import (
    EXIT_ERROR,
    EXIT_OK,
    DemoError,
    Engine,
    Scenario,
    tree_digest,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".demo-state",
    "reports",
    "htmlcov",
    ".coverage*",
    "*.db",
    "node_modules",
    "*.demo-tmp",
)
DIGEST_ENTRIES = ("app", "pyproject.toml", "uv.lock", "Dockerfile", "demo/scenarios.yaml")
COMMAND_TIMEOUT = 900
DOCKER_TIMEOUT = 2400
STARTUP_TIMEOUT = 30


# ---------------------------------------------------------------------------------------- results


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    id: str
    gate: str
    status: str = "FAIL"  # PASS | FAIL | SKIPPED
    reason: str = ""
    seconds: float = 0.0
    steps: list[Step] = field(default_factory=list)
    excerpt: str = ""

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append(Step(name, ok, detail))
        return ok

    def step_ok(self, name: str) -> str:
        for step in self.steps:
            if step.name == name:
                return "ok" if step.ok else "NO"
        return "-"


@dataclass
class Run:
    code: int
    output: str
    seconds: float


# --------------------------------------------------------------------------------------- helpers


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def tool_env(
    live_root: Path, *, sandbox: bool, extra: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(os.environ)
    local_bin = Path.home() / ".local" / "bin"
    env["PATH"] = f"{local_bin}{os.pathsep}{env.get('PATH', '')}"
    env["NO_COLOR"] = "1"
    env.pop("VIRTUAL_ENV", None)
    for key in ("STOCKFLOW_DEMO_FAULT", "STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES"):
        env.pop(key, None)  # never inherit a fault from the caller's shell
    if sandbox and (live_root / ".venv").is_dir():
        env["UV_PROJECT_ENVIRONMENT"] = str(live_root / ".venv")
        env["UV_NO_SYNC"] = "1"
    env.update(extra or {})
    return env


def run_shell(command: str, cwd: Path, env: dict[str, str], timeout: int = COMMAND_TIMEOUT) -> Run:
    start = time.monotonic()
    try:
        proc = subprocess.run(  # nosec B602 - commands come from scenarios.yaml, shell needed for $()
            command,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return Run(proc.returncode, strip_ansi(output), time.monotonic() - start)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"") + (exc.stderr or b"")
        text = partial.decode(errors="replace") if isinstance(partial, bytes) else str(partial)
        return Run(
            124, strip_ansi(text) + f"\n[timed out after {timeout}s]", time.monotonic() - start
        )


def tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def missing_signature(output: str, needles: list[str]) -> list[str]:
    return [needle for needle in needles if needle not in output]


def network_available() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=4):
            return True
    except OSError:
        return False


def docker_command() -> str | None:
    for candidate in (["docker"], ["sudo", "-n", "docker"]):
        if shutil.which(candidate[0]) is None:
            continue
        probe = subprocess.run(  # nosec B603 - fixed argv
            [*candidate, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if probe.returncode == 0:
            return " ".join(candidate)
    return None


def missing_requirements(scenario: Scenario, root: Path) -> list[str]:
    missing: list[str] = []
    for need in scenario.requires:
        if need == "network" and not network_available():
            missing.append("network (pypi.org unreachable)")
        elif need == "docker" and docker_command() is None:
            missing.append("docker (daemon not reachable, tried docker and sudo -n docker)")
        elif (
            need in ("trivy", "k6")
            and shutil.which(need, path=tool_env(root, sandbox=False)["PATH"]) is None
        ):
            missing.append(f"{need} (not installed)")
        elif need == "dockerfile" and not _dockerfile_matches(scenario, root):
            missing.append("Dockerfile (absent, or it does not follow the documented contract)")
    return missing


def _dockerfile_matches(scenario: Scenario, root: Path) -> bool:
    for op in scenario.patch:
        if op.kind != "replace_regex":
            continue
        path = root / op.file
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        if len(re.findall(op.pattern, text, re.MULTILINE)) != 1:
            return False
    return True


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextlib.contextmanager
def served_app(root: Path, env: dict[str, str]) -> Iterator[tuple[str, subprocess.Popen[str]]]:
    """Run ``python -m app`` on a free port; yield (base_url, process) once it listens."""
    port = free_port()
    full_env = {**env, "PORT": str(port), "HOST": "127.0.0.1"}
    log = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115  # closed in the finally block
    proc = subprocess.Popen(  # nosec B603 - fixed argv
        ["uv", "run", "python", "-m", "app"],  # nosec B607 - uv from PATH
        cwd=root,
        env=full_env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.seek(0)
                raise DemoError(
                    f"app exited with {proc.returncode} before listening:\n{tail(log.read())}"
                )
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
                break
            time.sleep(0.2)
        else:
            raise DemoError(f"app did not listen on port {port} within {STARTUP_TIMEOUT}s")
        yield f"http://127.0.0.1:{port}", proc
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
        log.close()


def http_probe(url: str, headers: dict[str, str] | None = None) -> tuple[int, float]:
    start = time.monotonic()
    request = urllib.request.Request(url, headers=headers or {})  # nosec B310 - local http
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
            response.read()
            return int(response.status), time.monotonic() - start
    except urllib.error.HTTPError as exc:
        return int(exc.code), time.monotonic() - start


# ------------------------------------------------------------------------------- the check engine


class Verifier:
    def __init__(self, engine: Engine, live_root: Path, *, sandbox: bool) -> None:
        self.engine = engine
        self.root = engine.root
        self.live_root = live_root
        self.sandbox = sandbox
        self.gates = engine.config.gates
        self.base_env = tool_env(live_root, sandbox=sandbox)
        self._docker = docker_command()

    # -- command plumbing
    def expand(self, command: str, base_url: str = "") -> str:
        docker = self._docker or "docker"
        return command.replace("{docker}", docker).replace("{base_url}", base_url)

    def run_gate(self, name: str) -> Run:
        return run_shell(self.gates[name], self.root, self.base_env)

    def run_expected(
        self, scenario: Scenario, command: str, env: dict[str, str] | None = None
    ) -> Run:
        timeout = DOCKER_TIMEOUT if "{docker}" in command else COMMAND_TIMEOUT
        merged = {**self.base_env, **(env or {})}
        return run_shell(self.expand(command), self.root, merged, timeout)

    # -- one scenario
    def verify(self, scenario: Scenario) -> Result:
        result = Result(scenario.id, str(scenario.expected["gate"]))
        started = time.monotonic()
        missing = missing_requirements(scenario, self.root)
        if missing:
            result.status = "SKIPPED"
            result.reason = "NOT VERIFIED, requires " + "; ".join(missing)
            return result
        try:
            self._verify_checked(scenario, result)
        except DemoError as exc:
            result.step("error", False, str(exc))
        finally:
            self._safe_reset(result)
            for command in scenario.expected.get("cleanup", []):
                run_shell(self.expand(command), self.root, self.base_env, 120)
        result.seconds = time.monotonic() - started
        failed = [s for s in result.steps if not s.ok]
        result.status = "FAIL" if failed else "PASS"
        if failed:
            result.reason = (
                f"{failed[0].name}: {failed[0].detail.splitlines()[0] if failed[0].detail else ''}"
            )
            result.excerpt = failed[0].detail
        return result

    def _safe_reset(self, result: Result) -> None:
        try:
            self.engine.reset(force=True)
        except DemoError as exc:
            result.step("final-reset", False, str(exc))

    def _verify_checked(self, scenario: Scenario, result: Result) -> None:
        if self.engine.read_state() is not None:
            raise DemoError("a scenario is already active in the work tree; reset it first")
        before = tree_digest(self.root, [e for e in DIGEST_ENTRIES if (self.root / e).exists()])
        if scenario.expected.get("exit") == "zero":
            self._verify_clean(scenario, result)
            return
        self.engine.inject(scenario.id, allow_runtime=True)
        result.step("inject", True)
        runtime = scenario.expected.get("runtime")
        if runtime:
            self._check_runtime(scenario, result, str(runtime))
        else:
            self._check_static(scenario, result)
        self.engine.reset()
        after = tree_digest(self.root, [e for e in DIGEST_ENTRIES if (self.root / e).exists()])
        result.step("reset-exact", before == after, f"tree digest {before[:12]} then {after[:12]}")
        if runtime:
            self._rerun_runtime_clean(scenario, result, str(runtime))
        else:
            self._rerun_static_clean(scenario, result)

    # -- clean baseline
    def _verify_clean(self, scenario: Scenario, result: Result) -> None:
        for gate in scenario.expected.get("gates", []):
            run = self.run_gate(gate)
            result.step(f"clean:{gate}", run.code == 0, f"exit {run.code}\n{tail(run.output)}")
        result.step(
            "no-markers", not self.engine.status().get("stray_markers"), "markers on clean tree"
        )

    # -- static (command-only) scenarios
    def _expect_failure(self, result: Result, label: str, run: Run, needles: list[str]) -> bool:
        failed = run.code != 0
        hits = [ln for ln in run.output.splitlines() if any(n in ln for n in needles)]
        evidence = f"exit {run.code}\n" + "\n".join((hits or run.output.splitlines())[-8:])
        ok = result.step(
            f"{label}:fails", failed, evidence if failed else f"expected non-zero, got {evidence}"
        )
        gaps = missing_signature(run.output, needles)
        detail = f"missing {gaps}\n{tail(run.output, 40)}" if gaps else f"matched {needles}"
        sig = result.step(f"{label}:signature", not gaps, detail)
        return ok and sig

    def _check_static(self, scenario: Scenario, result: Result) -> None:
        expected = scenario.expected
        run = self.run_expected(scenario, str(expected["command"]))
        self._expect_failure(result, "gate", run, list(expected["output_contains"]))
        for extra in expected.get("also", []):
            extra_run = self.run_expected(scenario, str(extra["command"]))
            self._expect_failure(
                result, f"also:{extra['gate']}", extra_run, list(extra["output_contains"])
            )
        for gate in scenario.must_still_pass:
            green = self.run_gate(gate)
            result.step(
                f"still-green:{gate}", green.code == 0, f"exit {green.code}\n{tail(green.output)}"
            )

    def _rerun_static_clean(self, scenario: Scenario, result: Result) -> None:
        expected = scenario.expected
        run = self.run_expected(scenario, str(expected["command"]))
        result.step("clean-rerun:gate", run.code == 0, f"exit {run.code}\n{tail(run.output)}")
        for extra in expected.get("also", []):
            extra_run = self.run_expected(scenario, str(extra["command"]))
            result.step(
                f"clean-rerun:{extra['gate']}",
                extra_run.code == 0,
                f"exit {extra_run.code}\n{tail(extra_run.output)}",
            )

    # -- runtime scenarios
    def _check_runtime(self, scenario: Scenario, result: Result, mode: str) -> None:
        expected = scenario.expected
        if mode == "startup":
            env = {**scenario.runtime_env, "PORT": str(free_port()), "HOST": "127.0.0.1"}
            run = self.run_expected(scenario, str(expected["command"]), env)
            self._expect_failure(result, "gate", run, list(expected["output_contains"]))
        else:
            with served_app(self.root, {**self.base_env, **scenario.runtime_env}) as (url, _proc):
                run = self.run_expected(
                    scenario, str(expected["command"]).replace("{base_url}", url)
                )
            self._expect_failure(result, "gate", run, list(expected["output_contains"]))
        self._check_inert(scenario, result, mode)
        for gate in scenario.must_still_pass:
            green = self.run_gate(gate)
            result.step(
                f"still-green:{gate}", green.code == 0, f"exit {green.code}\n{tail(green.output)}"
            )

    def _check_inert(self, scenario: Scenario, result: Result, mode: str) -> None:
        """The injected code must do nothing without BOTH env keys, and ignore request data."""
        probe = scenario.expected.get("inert_probe") or {"path": "/health", "max_seconds": 5.0}
        variants = {
            "no-env": {},
            "fault-key-only": {"STOCKFLOW_DEMO_FAULT": scenario.id},
            "allow-key-only": {"STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES": "true"},
        }
        for label, extra in variants.items():
            try:
                with served_app(self.root, {**self.base_env, **extra}) as (url, _proc):
                    status, elapsed = http_probe(url + str(probe["path"]))
                    forged_status, forged_elapsed = http_probe(
                        url
                        + str(probe["path"])
                        + f"?STOCKFLOW_DEMO_FAULT={scenario.id}&fault={scenario.id}",
                        {
                            "STOCKFLOW_DEMO_FAULT": scenario.id,
                            "X-Demo-Fault": scenario.id,
                            "X-STOCKFLOW-DEMO-ALLOW-RUNTIME-FAILURES": "true",
                        },
                    )
            except DemoError as exc:
                result.step(f"inert:{label}", False, str(exc))
                continue
            limit = float(probe.get("max_seconds", 5.0))
            healthy = status == 200 and forged_status == 200
            fast = elapsed < limit and forged_elapsed < limit
            result.step(
                f"inert:{label}",
                healthy and fast,
                (
                    f"status={status}/{forged_status} "
                    f"elapsed={elapsed:.2f}s/{forged_elapsed:.2f}s limit={limit}s"
                ),
            )

    def _rerun_runtime_clean(self, scenario: Scenario, result: Result, mode: str) -> None:
        expected = scenario.expected
        env = {**self.base_env, **scenario.runtime_env}
        if mode == "startup":
            with served_app(self.root, env):
                result.step("clean-rerun:gate", True)
            return
        with served_app(self.root, env) as (url, _proc):
            run = self.run_expected(scenario, str(expected["command"]).replace("{base_url}", url))
        result.step("clean-rerun:gate", run.code == 0, f"exit {run.code}\n{tail(run.output)}")


# ------------------------------------------------------------------------------------------ output


def _fails_cell(r: Result) -> str:
    if r.status == "SKIPPED":
        return "-"
    if any(s.name.startswith("clean:") for s in r.steps):
        return "n/a (all pass)" if all(s.ok for s in r.steps) else "NO"
    return r.step_ok("gate:fails")


def _group_cell(r: Result, prefix: str) -> str:
    group = [s for s in r.steps if s.name.startswith(prefix)]
    if not group:
        return "-"
    return "ok" if all(s.ok for s in group) else "NO"


def _rerun_cell(r: Result) -> str:
    group = [s for s in r.steps if s.name.startswith("clean-rerun:")]
    if not group:
        return "-"
    return "ok" if all(s.ok for s in group) else "NO"


COLUMNS = (
    ("SCENARIO", lambda r: r.id),
    ("GATE", lambda r: r.gate),
    ("FAILS-AS-EXPECTED", _fails_cell),
    ("SIGNATURE", lambda r: r.step_ok("gate:signature")),
    ("OTHER-GATES-GREEN", lambda r: _group_cell(r, "still-green:")),
    ("RESET-EXACT", lambda r: r.step_ok("reset-exact")),
    ("CLEAN-RERUN", _rerun_cell),
    ("RESULT", lambda r: r.status),
    ("SECS", lambda r: f"{r.seconds:.0f}"),
)


def format_table(results: list[Result]) -> str:
    rows = [[name for name, _ in COLUMNS]]
    for result in results:
        rows.append([str(getter(result)) for _, getter in COLUMNS])
    widths = [max(len(row[i]) for row in rows) for i in range(len(COLUMNS))]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def print_report(results: list[Result]) -> None:
    print()
    print(format_table(results))
    for result in results:
        if result.status == "SKIPPED":
            print(f"\n[{result.id}] SKIPPED: {result.reason}")
        elif result.status == "FAIL":
            print(f"\n[{result.id}] FAILED step -> {result.reason}")
            if result.excerpt:
                print("\n".join("    " + line for line in result.excerpt.splitlines()[:40]))


def run_verify(
    engine: Engine,
    ids: list[str],
    json_path: Path | None,
    *,
    strict: bool,
    in_place: bool = False,
) -> int:
    live = engine.root
    state = engine.read_state()
    if state is not None:
        raise DemoError(
            f"scenario '{state['scenario']}' is active in {live}; run `python -m demo reset` first "
            "(verification needs a clean baseline).",
            EXIT_ERROR,
        )
    scenarios = [engine.config.get(i) for i in ids]
    work: Path | None = None
    try:
        if in_place:
            worker = engine
        else:
            work = Path(tempfile.mkdtemp(prefix="stockflow-demo-verify-"))
            shutil.copytree(live, work, ignore=COPY_IGNORE, dirs_exist_ok=True, symlinks=True)
            (work / "reports").mkdir(exist_ok=True)
            worker = Engine(work)
            print(f"[demo] verifying in a sandbox copy: {work}")
        verifier = Verifier(worker, live, sandbox=not in_place)
        results: list[Result] = []
        for scenario in scenarios:
            print(f"[demo] === {scenario.id} ({scenario.expected['gate']}) ...", flush=True)
            result = verifier.verify(scenario)
            print(f"[demo] === {scenario.id}: {result.status} ({result.seconds:.0f}s)", flush=True)
            results.append(result)
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    print_report(results)
    target = json_path or (live / "reports" / "demo-verify.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n", encoding="utf-8")
    failed = [r for r in results if r.status == "FAIL"]
    skipped = [r for r in results if r.status == "SKIPPED"]
    print(
        f"\n[demo] {len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
        f"{len(skipped)} skipped (not verified). Details: {target}"
    )
    if failed or (strict and skipped):
        return EXIT_ERROR
    return EXIT_OK


# ------------------------------------------------------------------------------------ image guard


def image_guard(image: str, app_dir: str = "/app") -> int:
    """Fail if a built image contains demo tooling, fixtures, tests or DEMO-DEFECT markers."""
    docker = docker_command()
    if docker is None:
        raise DemoError("docker is not reachable; cannot inspect the image")
    script = (
        f"find {shlex.quote(app_dir)} -maxdepth 3 \\( -name demo -o -name fixtures -o -name tests "
        f"-o -name load-tests -o -name .demo-state -o -name scenarios.yaml \\) | head -20; "
        f"grep -rIl 'DEMO-DEFECT' {shlex.quote(app_dir)} 2>/dev/null | head -20"
    )
    argv = [*docker.split(), "run", "--rm", "--entrypoint", "sh", image, "-c", script]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)  # nosec B603
    if proc.returncode != 0:
        raise DemoError(f"could not inspect {image}: {proc.stderr.strip()}")
    findings = [line for line in proc.stdout.splitlines() if line.strip()]
    if findings:
        print(f"[demo] image-guard FAILED: {image} contains demo material:", file=sys.stderr)
        for line in findings:
            print(f"  {line}", file=sys.stderr)
        return EXIT_ERROR
    print(f"[demo] image-guard OK: {image} has no demo/, fixtures, tests or DEMO-DEFECT markers")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify defect scenarios against the real gates.")
    parser.add_argument("scenarios", nargs="*", help="scenario ids (default: all)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--in-place", action="store_true", help="use the live tree, not a copy")
    parser.add_argument("--strict", action="store_true", help="SKIPPED counts as failure")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    try:
        engine = Engine(args.root)
        ids = args.scenarios or [s.id for s in engine.config.scenarios]
        return run_verify(
            engine,
            ids,
            args.json,
            strict=args.strict or bool(args.scenarios),
            in_place=args.in_place,
        )
    except DemoError as exc:
        print(f"[demo] ERROR: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
