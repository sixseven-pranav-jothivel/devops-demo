"""Deterministic, reversible defect-injection engine.

Design (docs/ARCHITECTURE.md section 11):

* Scenarios are declared in ``demo/scenarios.yaml``; a scenario is a list of patch operations
  (``replace_text``, ``create_file``, ``replace_file``) applied to real files.
* ``inject`` is transactional and idempotent: backups and a write-ahead state file are written
  first, files are replaced atomically, and a failure rolls everything back.
* ``reset`` restores the exact pre-inject bytes (verified by sha256, independent of git) and
  refuses to touch a file the user edited after the injection unless ``force`` is given.
* Nothing here weakens a scanner or policy: the engine only edits files named by a scenario.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

MARKER_PREFIX = "DEMO-DEFECT:"
STATE_DIR_NAME = ".demo-state"
FIXTURES_DIR = Path("demo") / "fixtures"
DEFAULT_SCENARIOS = Path("demo") / "scenarios.yaml"
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
OP_KINDS = ("replace_text", "replace_regex", "create_file", "replace_file")
CATEGORIES = (
    "baseline",
    "logic",
    "lint",
    "format",
    "types",
    "security",
    "dependency",
    "container",
    "coverage",
    "runtime",
)
STAGES = ("pr-ci", "image-build", "post-deploy")
RUNTIME_ENV_KEY = "STOCKFLOW_DEMO_FAULT"
RUNTIME_ALLOW_KEY = "STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_UNVERIFIED = 3


class DemoError(Exception):
    """A user-facing failure. ``code`` is the process exit code."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


class RefusalError(DemoError):
    """The engine refused to act to protect user work (exit code 2)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_REFUSED)


# ------------------------------------------------------------------------------------------ model


@dataclass(frozen=True)
class Op:
    kind: str
    file: str
    old: str = ""
    new: str = ""
    pattern: str = ""
    content: str | None = None
    source: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    category: str
    description: str
    files: tuple[str, ...]
    patch: tuple[Op, ...]
    expected: dict[str, Any]
    reproduce: tuple[str, ...]
    remediation: str
    deployment_allowed: bool
    runtime: bool = False
    runtime_env: dict[str, str] = field(default_factory=dict)
    requires: tuple[str, ...] = ()
    must_still_pass: tuple[str, ...] = ()
    restore_note: str = ""


@dataclass(frozen=True)
class Config:
    allow_runtime_failures: bool
    gates: dict[str, str]
    fixture_checks: tuple[dict[str, Any], ...]
    scenarios: tuple[Scenario, ...]

    def get(self, scenario_id: str) -> Scenario:
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        known = ", ".join(s.id for s in self.scenarios)
        raise DemoError(f"Unknown scenario '{scenario_id}'. Known scenarios: {known}")


# -------------------------------------------------------------------------------------- utilities


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    """sha256 of a file, or None when it does not exist."""
    try:
        return sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write bytes via a temp file in the same directory and an atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".demo-tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def tree_digest(root: Path, entries: list[str]) -> str:
    """Stable digest over files/dirs (relative to root); used to prove reset is byte-exact."""
    digest = hashlib.sha256()
    for entry in sorted(entries):
        target = root / entry
        files = (
            [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
        )
        for file in files:
            rel = file.relative_to(root).as_posix()
            if "__pycache__" in rel or rel.endswith((".pyc", ".demo-tmp")):
                continue
            digest.update(rel.encode())
            digest.update(b"\0")
            digest.update(file.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _safe_relative(root: Path, rel: str, what: str) -> Path:
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts or not rel:
        raise DemoError(f"{what} '{rel}' must be a relative path without '..'.")
    resolved = (root / candidate).resolve()
    if root.resolve() not in resolved.parents and resolved != root.resolve():
        raise DemoError(f"{what} '{rel}' escapes the repository root.")
    return root / candidate


# ----------------------------------------------------------------------------- scenario loading


def _require(mapping: dict[str, Any], key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if key not in mapping:
        raise DemoError(f"{where}: missing required key '{key}'.")
    value = mapping[key]
    if not isinstance(value, kind):
        raise DemoError(f"{where}: '{key}' has the wrong type ({type(value).__name__}).")
    return value


def _parse_op(raw: dict[str, Any], where: str) -> Op:
    kind = _require(raw, "op", str, where)
    if kind not in OP_KINDS:
        raise DemoError(f"{where}: unknown op '{kind}' (allowed: {', '.join(OP_KINDS)}).")
    file = _require(raw, "file", str, where)
    if kind == "replace_regex":
        pattern = _require(raw, "pattern", str, where)
        try:
            re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            raise DemoError(f"{where}: invalid regex: {exc}") from exc
        return Op(kind, file, pattern=pattern, new=_require(raw, "new", str, where))
    if kind == "replace_text":
        old = _require(raw, "old", str, where)
        new = _require(raw, "new", str, where)
        if not old:
            raise DemoError(f"{where}: 'old' must not be empty.")
        return Op(kind, file, old=old, new=new)
    content = raw.get("content")
    source = raw.get("source")
    if (content is None) == (source is None):
        raise DemoError(f"{where}: {kind} needs exactly one of 'content' or 'source'.")
    return Op(kind, file, content=content, source=source, sha256=raw.get("sha256"))


def _parse_scenario(raw: dict[str, Any]) -> Scenario:
    sid = _require(raw, "id", str, "scenario")
    where = f"scenario '{sid}'"
    if not ID_RE.match(sid):
        raise DemoError(f"{where}: id must be snake_case.")
    category = _require(raw, "category", str, where)
    if category not in CATEGORIES:
        raise DemoError(f"{where}: unknown category '{category}'.")
    expected = _require(raw, "expected", dict, where)
    _require(expected, "gate", str, f"{where}.expected")
    _require(expected, "command", str, f"{where}.expected")
    _require(expected, "output_contains", list, f"{where}.expected")
    if expected.get("stage") not in STAGES:
        raise DemoError(f"{where}.expected: stage must be one of {', '.join(STAGES)}.")
    patch_raw = _require(raw, "patch", list, where)
    ops = tuple(_parse_op(p, f"{where}.patch[{i}]") for i, p in enumerate(patch_raw))
    files = tuple(_require(raw, "files", list, where))
    reproduce = tuple(_require(raw, "reproduce", list, where))
    if not reproduce:
        raise DemoError(f"{where}: 'reproduce' must list at least one command.")
    runtime = category == "runtime"
    runtime_env = dict(raw.get("runtime_env") or {})
    scenario = Scenario(
        id=sid,
        name=_require(raw, "name", str, where),
        category=category,
        description=_require(raw, "description", str, where),
        files=files,
        patch=ops,
        expected=expected,
        reproduce=reproduce,
        remediation=_require(raw, "remediation", str, where),
        deployment_allowed=_require(raw, "deployment_allowed", bool, where),
        runtime=runtime,
        runtime_env={str(k): str(v) for k, v in runtime_env.items()},
        requires=tuple(raw.get("requires") or ()),
        must_still_pass=tuple(raw.get("must_still_pass") or ()),
        restore_note=str(raw.get("restore_note", "")),
    )
    _validate_scenario(scenario)
    return scenario


def _validate_scenario(s: Scenario) -> None:
    where = f"scenario '{s.id}'"
    touched = {op.file for op in s.patch}
    if touched != set(s.files):
        raise DemoError(
            f"{where}: 'files' {sorted(s.files)} must equal the files touched by 'patch' "
            f"{sorted(touched)}."
        )
    marker = MARKER_PREFIX + s.id
    for op in s.patch:
        if op.kind in ("replace_text", "replace_regex") and marker not in op.new:
            raise DemoError(f"{where}: every {op.kind} 'new' must contain '{marker}'.")
        if (
            op.kind in ("create_file", "replace_file")
            and op.content is not None
            and marker not in op.content
        ):
            raise DemoError(f"{where}: inline 'content' must contain '{marker}'.")
    if s.runtime:
        if s.runtime_env.get(RUNTIME_ENV_KEY) != s.id:
            raise DemoError(f"{where}: runtime_env must set {RUNTIME_ENV_KEY}={s.id}.")
        if s.runtime_env.get(RUNTIME_ALLOW_KEY) != "true":
            raise DemoError(f"{where}: runtime_env must set {RUNTIME_ALLOW_KEY}=true.")
        if s.expected.get("stage") != "post-deploy":
            raise DemoError(f"{where}: runtime scenarios fail at stage post-deploy.")
    elif s.runtime_env:
        raise DemoError(f"{where}: runtime_env is only valid for category 'runtime'.")


def load_config(root: Path, scenarios_file: Path | None = None) -> Config:
    path = scenarios_file or (root / DEFAULT_SCENARIOS)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DemoError(f"Scenario file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise DemoError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise DemoError(f"{path}: expected a mapping with 'version: 1'.")
    demo_section = raw.get("demo") or {}
    scenarios = tuple(_parse_scenario(s) for s in _require(raw, "scenarios", list, str(path)))
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise DemoError(f"{path}: scenario ids must be unique.")
    return Config(
        allow_runtime_failures=bool(demo_section.get("allow_runtime_failures", False)),
        gates=dict(raw.get("gates") or {}),
        fixture_checks=tuple(raw.get("fixture_checks") or ()),
        scenarios=scenarios,
    )


# ------------------------------------------------------------------------------------- the engine


@dataclass
class Report:
    """What an engine action did (printed by the CLI, asserted by tests)."""

    action: str
    scenario: str | None
    changed: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, root: Path, scenarios_file: Path | None = None) -> None:
        self.root = root.resolve()
        self.config = load_config(self.root, scenarios_file)
        self.state_dir = self.root / STATE_DIR_NAME
        self.state_path = self.state_dir / "state.json"

    # ---- state helpers
    def read_state(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise DemoError(
                f"{self.state_path} is corrupt ({exc}). Restore the files named in "
                f"{STATE_DIR_NAME}/backup/ manually, then delete {STATE_DIR_NAME}/.",
                EXIT_ERROR,
            ) from exc
        assert isinstance(data, dict)
        return data

    def _write_state(self, state: dict[str, Any]) -> None:
        atomic_write(self.state_path, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        """Exclusive advisory lock (flock) on .demo-state/lock; leaves nothing behind when idle.

        The lock is a kernel-level flock on an open descriptor, so the file merely existing never
        blocks anything: if a run crashes or is killed the kernel drops its lock, and the next run
        acquires it immediately (a leftover `lock` file is "stale" and harmless). After acquiring,
        the path's inode is compared with the descriptor's so a lock file unlinked by a finishing
        run is never mistaken for the live one.
        """
        lock_path = self.state_dir / "lock"
        while True:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115  # closed in finally
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                same = os.fstat(handle.fileno()).st_ino == os.stat(lock_path).st_ino
            except FileNotFoundError:
                same = False
            if same:
                break
            handle.close()  # the previous holder removed the file while we waited: retry
        try:
            yield
        finally:
            self._tidy()
            handle.close()  # releases the flock

    def _tidy(self) -> None:
        """Remove .demo-state when nothing is worth keeping (no state file, no discarded edits)."""
        if self.state_path.exists():
            return
        backup = self.state_dir / "backup"
        if backup.is_dir() and not any(p.is_file() for p in backup.rglob("*")):
            shutil.rmtree(backup, ignore_errors=True)
        discarded = self.state_dir / "discarded"
        if discarded.is_dir() and not any(p.is_file() for p in discarded.rglob("*")):
            shutil.rmtree(discarded, ignore_errors=True)
        with contextlib.suppress(FileNotFoundError):
            (self.state_dir / "lock").unlink()
        with contextlib.suppress(OSError):  # non-empty: something worth keeping stays
            self.state_dir.rmdir()

    def _backup_path(self, scenario_id: str, rel: str) -> Path:
        return self.state_dir / "backup" / scenario_id / rel

    # ---- patch computation (pure; no writes)
    def _read_source(self, scenario: Scenario, op: Op) -> str:
        assert op.source is not None
        base = self.root / FIXTURES_DIR / scenario.id
        path = _safe_relative(base, op.source, "fixture source")
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise DemoError(f"{scenario.id}: fixture file missing: {path}") from exc
        if op.sha256 is not None and sha256_bytes(data) != op.sha256:
            raise DemoError(
                f"{scenario.id}: fixture {op.source} sha256 mismatch (expected {op.sha256}, got "
                f"{sha256_bytes(data)}). The fixture changed; refusing to inject it."
            )
        return data.decode("utf-8")

    def compute(self, scenario: Scenario, *, force: bool = False) -> dict[str, bytes]:
        """Final bytes for every touched file. Raises DemoError if any op cannot apply cleanly."""
        current: dict[str, bytes | None] = {}
        for op in scenario.patch:
            _safe_relative(self.root, op.file, "patch file")
            if op.file not in current:
                path = self.root / op.file
                current[op.file] = path.read_bytes() if path.is_file() else None
        for index, op in enumerate(scenario.patch):
            before = current[op.file]
            where = f"{scenario.id} patch[{index}] {op.kind} {op.file}"
            if op.kind == "replace_text":
                current[op.file] = self._apply_replace_text(where, before, op)
            elif op.kind == "replace_regex":
                current[op.file] = self._apply_replace_regex(where, before, op)
            elif op.kind == "create_file":
                text = op.content if op.content is not None else self._read_source(scenario, op)
                if before is not None and before != text.encode() and not force:
                    raise RefusalError(
                        f"{where}: file already exists with different content. Refusing to "
                        "overwrite it. Move it away, or pass --force (the original is backed up)."
                    )
                current[op.file] = text.encode()
            else:  # replace_file
                if before is None:
                    raise DemoError(f"{where}: file does not exist (use create_file).")
                text = op.content if op.content is not None else self._read_source(scenario, op)
                current[op.file] = text.encode()
        result: dict[str, bytes] = {}
        for rel, data in current.items():
            assert data is not None
            result[rel] = data
        return result

    @staticmethod
    def _apply_replace_text(where: str, before: bytes | None, op: Op) -> bytes:
        if before is None:
            raise DemoError(f"{where}: file does not exist.")
        text = before.decode("utf-8")
        count = text.count(op.old)
        if count != 1:
            raise DemoError(
                f"{where}: expected the 'old' text to occur exactly once, found {count}. "
                "The file differs from the baseline this scenario was written for (a user edit, "
                "an upstream change, or a defect that is already applied). Restore the file or "
                "update the scenario."
            )
        return text.replace(op.old, op.new, 1).encode()

    @staticmethod
    def _apply_replace_regex(where: str, before: bytes | None, op: Op) -> bytes:
        if before is None:
            raise DemoError(f"{where}: file does not exist.")
        text = before.decode("utf-8")
        matches = list(re.finditer(op.pattern, text, re.MULTILINE))
        if len(matches) != 1:
            raise DemoError(
                f"{where}: expected the pattern {op.pattern!r} to match exactly once, found "
                f"{len(matches)}. The file does not follow the documented contract (see "
                "demo/README.md) or a defect is already applied."
            )
        match = matches[0]
        return (text[: match.start()] + op.new + text[match.end() :]).encode()

    # ---- inject
    def inject(
        self, scenario_id: str, *, force: bool = False, allow_runtime: bool = False
    ) -> Report:
        scenario = self.config.get(scenario_id)
        if scenario.runtime and not (allow_runtime or self.config.allow_runtime_failures):
            raise RefusalError(
                f"'{scenario.id}' is a runtime-failure scenario and runtime failures are disabled "
                "(demo.allow_runtime_failures: false in demo/scenarios.yaml). Re-run with "
                "--allow-runtime-failures to acknowledge that. The injected code still stays "
                f"inert unless the process also sets {RUNTIME_ENV_KEY}={scenario.id} and "
                f"{RUNTIME_ALLOW_KEY}=true."
            )
        with self._lock():
            state = self.read_state()
            if state is not None:
                same = state.get("scenario") == scenario.id and state.get("status") == "active"
                if same:
                    problems = self._modified_since_inject(state)
                    if problems and not force:
                        raise RefusalError(self._edit_message(problems, "re-inject"))
                    if not problems and self._all_injected(state):
                        return Report("inject", scenario.id, [], ["already active (no-op)"])
                self._reset_locked(force=force)
            elif not force:
                self._refuse_if_orphan_markers(scenario)
            if scenario.id == "clean":
                return Report("inject", scenario.id, [], ["baseline restored (nothing to patch)"])
            return self._apply_locked(scenario, force=force)

    def _refuse_if_orphan_markers(self, scenario: Scenario) -> None:
        orphans = [rel for rel in scenario.files if self._has_marker(self.root / rel)]
        if orphans:
            raise RefusalError(
                f"{', '.join(orphans)} already contain {MARKER_PREFIX} markers but there is no "
                f"{STATE_DIR_NAME}/state.json, so the engine cannot restore them. The tree is not "
                "at baseline (a defect was committed or the state was deleted). Restore the files "
                "from version control first."
            )

    @staticmethod
    def _has_marker(path: Path) -> bool:
        try:
            return MARKER_PREFIX in path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            return False

    def _apply_locked(self, scenario: Scenario, *, force: bool) -> Report:
        new_contents = self.compute(scenario, force=force)
        files: dict[str, dict[str, Any]] = {}
        created_dirs: list[str] = []
        for rel, data in new_contents.items():
            path = self.root / rel
            before = path.read_bytes() if path.is_file() else None
            if before is None:
                created_dirs.extend(self._missing_dirs(path))
            elif before == data:
                raise DemoError(f"{scenario.id}: patch leaves {rel} unchanged; nothing to inject.")
            files[rel] = {
                "existed": before is not None,
                "before_sha256": sha256_bytes(before) if before is not None else None,
                "after_sha256": sha256_bytes(data),
                "mode": path.stat().st_mode & 0o777 if before is not None else None,
            }
            if before is not None:
                atomic_write(self._backup_path(scenario.id, rel), before)
        state: dict[str, Any] = {
            "version": 1,
            "scenario": scenario.id,
            "status": "applying",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "files": files,
            "created_dirs": sorted(set(created_dirs)),
        }
        self._write_state(state)
        try:
            for rel, data in new_contents.items():
                atomic_write(self.root / rel, data, files[rel]["mode"] or 0o644)
        except BaseException:
            self._reset_locked(force=True)
            raise
        state["status"] = "active"
        self._write_state(state)
        return Report("inject", scenario.id, sorted(new_contents), ["injected"])

    def _missing_dirs(self, path: Path) -> list[str]:
        missing: list[str] = []
        parent = path.parent
        while not parent.exists() and parent != self.root:
            missing.append(parent.relative_to(self.root).as_posix())
            parent = parent.parent
        return missing

    # ---- reset
    def reset(self, *, force: bool = False) -> Report:
        with self._lock():
            return self._reset_locked(force=force)

    def _modified_since_inject(self, state: dict[str, Any]) -> list[str]:
        """Files whose current hash is neither the injected nor the original content."""
        problems: list[str] = []
        for rel, info in state["files"].items():
            current = sha256_file(self.root / rel)
            if current not in (info["after_sha256"], info["before_sha256"]):
                problems.append(rel)
        return sorted(problems)

    def _all_injected(self, state: dict[str, Any]) -> bool:
        return state.get("status") == "active" and all(
            sha256_file(self.root / rel) == info["after_sha256"]
            for rel, info in state["files"].items()
        )

    @staticmethod
    def _edit_message(problems: list[str], action: str) -> str:
        listed = "\n".join(f"  - {p}" for p in problems)
        return (
            f"Refusing to {action}: these files were edited after the defect was injected and "
            f"their current content is neither the injected nor the original version:\n{listed}\n"
            "Your edits would be lost. Commit or copy them elsewhere, or re-run with --force "
            f"(edited copies are kept under {STATE_DIR_NAME}/discarded/)."
        )

    def _reset_locked(self, *, force: bool) -> Report:
        state = self.read_state()
        if state is None:
            return Report("reset", None, [], ["no active scenario; nothing to reset"])
        scenario_id = str(state["scenario"])
        problems = self._modified_since_inject(state)
        if problems and not force:
            raise RefusalError(self._edit_message(problems, "reset"))
        changed: list[str] = []
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for rel, info in sorted(state["files"].items()):
            path = self.root / rel
            current = sha256_file(path)
            if current == info["before_sha256"]:
                continue  # already at baseline (or absent when the file did not exist)
            if current is not None and current != info["after_sha256"]:
                self._keep_discarded(path, rel, stamp)  # only reachable with force
            self._restore_one(scenario_id, rel, info)
            changed.append(rel)
        for rel in sorted(state.get("created_dirs", []), reverse=True):
            with contextlib.suppress(OSError):
                (self.root / rel).rmdir()
        shutil.rmtree(self.state_dir / "backup" / scenario_id, ignore_errors=True)
        self.state_path.unlink()
        return Report("reset", scenario_id, changed, ["restored baseline"])

    def _keep_discarded(self, path: Path, rel: str, stamp: str) -> None:
        if path.is_file():
            atomic_write(self.state_dir / "discarded" / stamp / rel, path.read_bytes())

    def _restore_one(self, scenario_id: str, rel: str, info: dict[str, Any]) -> None:
        path = self.root / rel
        if not info["existed"]:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return
        backup = self._backup_path(scenario_id, rel)
        try:
            data = backup.read_bytes()
        except FileNotFoundError as exc:
            raise DemoError(
                f"Backup for {rel} is missing ({backup}); restore it from git."
            ) from exc
        if sha256_bytes(data) != info["before_sha256"]:
            raise DemoError(f"Backup for {rel} failed its sha256 check; restore it from git.")
        atomic_write(path, data, info.get("mode"))
        if sha256_file(path) != info["before_sha256"]:
            raise DemoError(f"Restored {rel} does not match its baseline hash.")

    # ---- status
    def status(self) -> dict[str, Any]:
        state = self.read_state()
        if state is None:
            stray = sorted(self._scan_markers())
            return {"active": None, "files": {}, "stray_markers": stray}
        files: dict[str, str] = {}
        for rel, info in state["files"].items():
            current = sha256_file(self.root / rel)
            if current == info["after_sha256"]:
                files[rel] = "injected"
            elif current == info["before_sha256"]:
                files[rel] = "baseline"
            else:
                files[rel] = "MODIFIED"
        return {"active": state["scenario"], "status": state["status"], "files": files}

    def _scan_markers(self) -> Iterator[str]:
        seen: set[str] = set()
        for scenario in self.config.scenarios:
            for rel in scenario.files:
                if rel not in seen and self._has_marker(self.root / rel):
                    seen.add(rel)
                    yield rel
