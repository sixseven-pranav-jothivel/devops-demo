#!/usr/bin/env python3
# ruff: noqa: E501  (long human-readable messages and regexes are clearer unwrapped)
"""Single entry point to StockFlow's quality policy (``config/quality-policy.yaml``).

Dependencies: Python standard library + PyYAML only (PyYAML is in the dev dependency group).

Sub-commands
------------
``get KEY``            print one value by dotted key (``coverage.fail_under``, ``trivy.image.severities``).
                       Lists print comma-joined, booleans as ``true``/``false``, mappings as JSON.
``args TOOL``          print ready-to-use, shell-quoted command-line flags for a tool so that no
                       threshold is ever typed in a Makefile or workflow. TOOL is one of
                       ``bandit pip-audit uv-export trivy-fs trivy-image trivy-image-report
                       trivy-config k6``.
``check``              validate the policy and everything that must agree with it (see ``run_check``).

Exit codes: 0 ok, 1 ``check`` found problems, 2 usage / unreadable or malformed policy / unknown key.

Library use (tests): every check is a pure function taking explicit ``root``/``today`` arguments and
returning a list of human-readable problem strings; nothing touches the network or the clock unless
``today`` is omitted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY = Path("config/quality-policy.yaml")
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
BANDIT_LEVELS = {"low": 1, "medium": 2, "high": 3}
TOOLS = (
    "bandit",
    "pip-audit",
    "uv-export",
    "trivy-fs",
    "trivy-image",
    "trivy-image-report",
    "trivy-config",
    "k6",
)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class PolicyError(Exception):
    """The policy file is unreadable, malformed or a requested key does not exist."""


# --------------------------------------------------------------------------- YAML loading


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (a duplicate silently overrides a threshold)."""


def _construct_mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen: dict[Any, int] = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise PolicyError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1} "
                f"(first defined at line {seen[key]})"
            )
        seen[key] = key_node.start_mark.line + 1
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)  # type: ignore[arg-type]


def load_yaml(text: str, source: str = "<string>") -> Any:
    """Parse YAML, raising ``PolicyError`` on syntax errors or duplicate keys."""
    try:
        return yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{source}: invalid YAML: {exc}") from exc
    except PolicyError as exc:
        raise PolicyError(f"{source}: {exc}") from exc


def load_policy(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read policy {path}: {exc}") from exc
    data = load_yaml(text, str(path))
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: top level must be a mapping")
    return data


# --------------------------------------------------------------------------- get / format


def get_value(policy: dict[str, Any], key: str) -> Any:
    """Resolve a dotted key; numeric segments index into lists."""
    node: Any = policy
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise PolicyError(f"unknown policy key: {key}")
    return node


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(format_value(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


# --------------------------------------------------------------------------- dates / ignores


def to_date(value: Any) -> dt.date | None:
    """Accept YAML dates (datetime.date) and ISO strings; return None when not a valid date."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def check_ignore_entries(entries: Any, where: str, today: dt.date, max_days: int) -> list[str]:
    """Validate ``[{id, reason, expires}]``: required fields, unique ids, not expired, bounded expiry."""
    if not isinstance(entries, list):
        return [f"{where}: must be a list"]
    problems: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        label = f"{where}[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{label}: must be a mapping with id, reason, expires")
            continue
        missing = {"id", "reason", "expires"} - set(entry)
        extra = set(entry) - {"id", "reason", "expires"}
        if missing:
            problems.append(
                f"{label}: missing {sorted(missing)} (id + reason + expires are required)"
            )
        if extra:
            problems.append(f"{label}: unknown keys {sorted(extra)}")
        ident = entry.get("id")
        if not isinstance(ident, str) or not ID_RE.match(ident):
            problems.append(f"{label}: id {ident!r} is not a valid advisory id")
        elif ident in seen:
            problems.append(f"{label}: duplicate ignore id {ident}")
        else:
            seen.add(ident)
        reason = entry.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < 15:
            problems.append(f"{label}: reason must be a meaningful sentence (>= 15 characters)")
        if "expires" in entry:
            problems.extend(_check_expiry(entry["expires"], label, today, max_days))
    return problems


def _check_expiry(raw: Any, label: str, today: dt.date, max_days: int) -> list[str]:
    expires = to_date(raw)
    if expires is None:
        return [f"{label}: expires {raw!r} is not an ISO date (YYYY-MM-DD)"]
    if expires < today:
        return [
            f"{label}: EXPIRED on {expires} (today {today}); fix the finding or renew with a new reason"
        ]
    if (expires - today).days > max_days:
        return [f"{label}: expires {expires}, more than {max_days} days ahead (max allowed window)"]
    return []


def active_pip_audit_ignores(policy: dict[str, Any], today: dt.date) -> list[str]:
    """Ids of non-expired pip-audit ignores (expired ones are deliberately NOT returned)."""
    out: list[str] = []
    for entry in policy.get("pip_audit", {}).get("ignore", []) or []:
        expires = to_date(entry.get("expires"))
        if expires is not None and expires >= today:
            out.append(str(entry["id"]))
    return out


TRIVY_ENTRY_RE = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\s+exp:(?P<exp>\S+))?\s*$")


def check_trivyignore(
    text: str, today: dt.date, max_days: int, name: str = ".trivyignore"
) -> list[str]:
    """Every entry needs a preceding ``# justification: ...`` comment and an ``exp:YYYY-MM-DD`` date."""
    problems: list[str] = []
    justification = ""
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        label = f"{name}:{lineno}"
        if not line:
            justification = ""
            continue
        if line.startswith("#"):
            m = re.match(r"^#\s*justification:\s*(.*)$", line, re.IGNORECASE)
            if m:
                justification = m.group(1).strip()
            continue
        m = TRIVY_ENTRY_RE.match(line)
        if not m:
            problems.append(f"{label}: malformed entry {line!r} (expected '<ID> exp:YYYY-MM-DD')")
        else:
            ident = m.group("id")
            if ident in seen:
                problems.append(f"{label}: duplicate entry {ident}")
            seen.add(ident)
            if len(justification) < 15:
                problems.append(
                    f"{label}: {ident} lacks a preceding '# justification: <reason>' comment"
                )
            if m.group("exp") is None:
                problems.append(f"{label}: {ident} has no 'exp:YYYY-MM-DD' expiry")
            else:
                problems.extend(_check_expiry(m.group("exp"), f"{label} {ident}", today, max_days))
        justification = ""
    return problems


# --------------------------------------------------------------------------- schema

Validator = Any  # callable(value) -> str | None, or a nested dict of validators


def _bool(v: Any) -> str | None:
    return None if isinstance(v, bool) else f"expected true/false, got {v!r}"


def _str(v: Any) -> str | None:
    return None if isinstance(v, str) and v.strip() else f"expected non-empty string, got {v!r}"


def _int(lo: int, hi: int | None = None) -> Validator:
    def check(v: Any) -> str | None:
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)
        return (
            None
            if ok
            else f"expected integer >= {lo}{'' if hi is None else f' and <= {hi}'}, got {v!r}"
        )

    return check


def _num(lo: float, hi: float) -> Validator:
    def check(v: Any) -> str | None:
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi
        return None if ok else f"expected number in [{lo}, {hi}], got {v!r}"

    return check


def _enum(options: set[str]) -> Validator:
    return lambda v: None if v in options else f"expected one of {sorted(options)}, got {v!r}"


def _eq(expected: Any) -> Validator:
    return lambda v: (
        None if v == expected and type(v) is type(expected) else f"must be {expected!r}, got {v!r}"
    )


def _list(item: Validator = _str, *, nonempty: bool = True) -> Validator:
    def check(v: Any) -> str | None:
        if not isinstance(v, list):
            return f"expected a list, got {v!r}"
        if nonempty and not v:
            return "must not be empty"
        for i, el in enumerate(v):
            err = item(el)
            if err:
                return f"item {i}: {err}"
        if len({json.dumps(x, sort_keys=True, default=str) for x in v}) != len(v):
            return "contains duplicate items"
        return None

    return check


def _any(_: Any) -> None:
    return None


def _dict_of(value: Validator) -> Validator:
    def check(v: Any) -> str | None:
        if not isinstance(v, dict) or not v:
            return f"expected a non-empty mapping, got {v!r}"
        for k, val in v.items():
            err = value(val)
            if err:
                return f"{k}: {err}"
        return None

    return check


_SEVERITY_LIST = _list(_enum(SEVERITIES))

SCHEMA: dict[str, Validator] = {
    "version": _eq(1),
    "python": {
        "version": lambda v: (
            None
            if isinstance(v, str) and re.fullmatch(r"3\.\d+", v)
            else f"expected '3.N' string, got {v!r}"
        )
    },
    "coverage": {"fail_under": _num(0, 100), "branch": _bool, "source": _list()},
    "ruff": {"paths": _list(), "check": _bool, "format_check": _bool},
    "mypy": {"strict": _bool, "paths": _list()},
    "bandit": {
        "paths": _list(),
        "config": _str,
        "severity": _enum(set(BANDIT_LEVELS)),
        "confidence": _enum(set(BANDIT_LEVELS)),
        "fail_on_findings": _bool,
    },
    "pip_audit": {
        "scope": _enum({"runtime", "runtime+dev"}),
        "requirements_file": _str,
        "fail_on_any": _bool,
        "ignore": _any,  # validated by check_ignore_entries
        "ignore_max_days": _int(1, 365),
    },
    "trivy": {
        "scanners_fs": _list(_enum({"vuln", "secret", "misconfig", "license"})),
        "skip_dirs": _list(),
        "fs": {
            "severities": _SEVERITY_LIST,
            "ignore_unfixed": _bool,
            "include_dev_deps": _bool,
            "exit_code": _int(0, 1),
        },
        "image": {
            "severities": _SEVERITY_LIST,
            "ignore_unfixed": _bool,
            "exit_code": _int(0, 1),
            "platform": _str,
            "scanners": _list(_enum({"vuln", "secret", "misconfig", "license"})),
        },
        "config": {"severities": _SEVERITY_LIST, "exit_code": _int(0, 1), "paths": _list()},
        "report_formats": _list(_enum({"table", "json"})),
        "ignorefile": _str,
        "ignore_max_days": _int(1, 365),
        "config_file": _str,
    },
    "actions": {
        "pin_to_full_sha": _bool,
        "default_permissions": _dict_of(_enum({"read", "none"})),
        "forbid_triggers": _list(_str, nonempty=False),
        "allowed_job_write_permissions": _dict_of(_list(_str, nonempty=False)),
        "forbid_continue_on_error": _bool,
        "secrets_require_environment": _bool,
        "secret_free_workflows": _list(_str, nonempty=False),
    },
    "smoke": {"timeout_seconds": _int(1), "poll_interval_seconds": _int(1)},
    "load_test": {
        "p95_ms": _int(1),
        "error_rate_max": _num(0, 1),
        "vus": _int(1),
        "duration": _str,
    },
    "drift_scan": {"paths": _list()},
}


def validate_schema(node: Any, schema: Any = None, path: str = "") -> list[str]:
    """Check types/enums, report missing and unknown keys. Returns problem strings."""
    schema = SCHEMA if schema is None else schema
    problems: list[str] = []
    if isinstance(schema, dict):
        if not isinstance(node, dict):
            return [f"{path or '<root>'}: expected a mapping"]
        for key in schema:
            if key not in node:
                problems.append(f"{path}{key}: missing required key")
        for key, value in node.items():
            if key not in schema:
                problems.append(f"{path}{key}: unknown key (add it to SCHEMA if intentional)")
            else:
                problems.extend(validate_schema(value, schema[key], f"{path}{key}."))
        return problems
    err = schema(node)
    return [f"{path.rstrip('.')}: {err}"] if err else []


# --------------------------------------------------------------------------- floors (anti-weakening)


def _sev_has_blockers(v: Any) -> bool:
    return isinstance(v, list) and {"HIGH", "CRITICAL"} <= set(v)


def _lvl(name: str) -> Any:
    return lambda v: BANDIT_LEVELS.get(v, 99) <= BANDIT_LEVELS[name]


# (dotted key, predicate, human description). Lowering any of these needs a deliberate edit HERE too.
FLOORS: list[tuple[str, Any, str]] = [
    ("coverage.fail_under", lambda v: isinstance(v, (int, float)) and v >= 85, ">= 85"),
    ("coverage.branch", lambda v: v is True, "true"),
    ("mypy.strict", lambda v: v is True, "true"),
    ("bandit.severity", _lvl("medium"), "medium or lower (stricter)"),
    ("bandit.confidence", _lvl("medium"), "medium or lower (stricter)"),
    ("bandit.fail_on_findings", lambda v: v is True, "true"),
    ("pip_audit.fail_on_any", lambda v: v is True, "true"),
    ("pip_audit.scope", lambda v: v == "runtime+dev", "runtime+dev"),
    ("pip_audit.ignore_max_days", lambda v: isinstance(v, int) and v <= 90, "<= 90"),
    ("trivy.fs.severities", _sev_has_blockers, "contains HIGH and CRITICAL"),
    ("trivy.image.severities", _sev_has_blockers, "contains HIGH and CRITICAL"),
    ("trivy.config.severities", _sev_has_blockers, "contains HIGH and CRITICAL"),
    ("trivy.fs.exit_code", lambda v: v == 1, "1 (blocking)"),
    ("trivy.image.exit_code", lambda v: v == 1, "1 (blocking)"),
    ("trivy.config.exit_code", lambda v: v == 1, "1 (blocking)"),
    ("trivy.fs.ignore_unfixed", lambda v: v is False, "false (strict)"),
    ("trivy.ignore_max_days", lambda v: isinstance(v, int) and v <= 90, "<= 90"),
    ("actions.pin_to_full_sha", lambda v: v is True, "true"),
    ("actions.default_permissions", lambda v: v == {"contents": "read"}, "{contents: read}"),
    ("actions.forbid_continue_on_error", lambda v: v is True, "true"),
    ("actions.secrets_require_environment", lambda v: v is True, "true"),
    (
        "actions.forbid_triggers",
        lambda v: isinstance(v, list) and "pull_request_target" in v,
        "contains pull_request_target",
    ),
    ("load_test.p95_ms", lambda v: isinstance(v, int) and v <= 500, "<= 500"),
    ("load_test.error_rate_max", lambda v: isinstance(v, (int, float)) and v <= 0.01, "<= 0.01"),
]


def check_floors(policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, pred, desc in FLOORS:
        try:
            value = get_value(policy, key)
        except PolicyError:
            continue  # reported by the schema check
        if not pred(value):
            problems.append(f"{key}: {value!r} is weaker than the policy floor ({desc})")
    return problems


# --------------------------------------------------------------------------- mirrors / cross-file


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None


def check_mirrors(root: Path, policy: dict[str, Any]) -> list[str]:
    """pyproject.toml / .python-version / tool config files must agree with the policy."""
    problems: list[str] = []
    pyproject = _read_toml(root / "pyproject.toml")
    if pyproject is None:
        problems.append("pyproject.toml: missing or unparsable")
    else:
        tool = pyproject.get("tool", {})
        cov = tool.get("coverage", {})
        pol_cov = policy.get("coverage", {})
        got = cov.get("report", {}).get("fail_under")
        if got != pol_cov.get("fail_under"):
            problems.append(
                f"pyproject [tool.coverage.report] fail_under={got!r} != policy coverage.fail_under="
                f"{pol_cov.get('fail_under')!r}"
            )
        if cov.get("run", {}).get("branch") is not pol_cov.get("branch"):
            problems.append(
                "pyproject [tool.coverage.run] branch differs from policy coverage.branch"
            )
        if sorted(cov.get("run", {}).get("source", [])) != sorted(pol_cov.get("source", [])):
            problems.append(
                "pyproject [tool.coverage.run] source differs from policy coverage.source"
            )
        mypy = tool.get("mypy", {})
        if mypy.get("strict") is not policy.get("mypy", {}).get("strict"):
            problems.append("pyproject [tool.mypy] strict differs from policy mypy.strict")
        if sorted(mypy.get("files", [])) != sorted(policy.get("mypy", {}).get("paths", [])):
            problems.append("pyproject [tool.mypy] files differ from policy mypy.paths")
        addopts = str(tool.get("pytest", {}).get("ini_options", {}).get("addopts", ""))
        if re.search(r"--cov-fail-under", addopts):
            problems.append(
                "pyproject pytest addopts hard-codes --cov-fail-under; use [tool.coverage.report]"
            )
        problems.extend(_check_python_version(root, policy, pyproject))
    problems.extend(_check_tool_config_files(root, policy))
    return problems


def _check_python_version(
    root: Path, policy: dict[str, Any], pyproject: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    version = str(policy.get("python", {}).get("version", ""))
    pv_file = root / ".python-version"
    if pv_file.exists():
        pv = pv_file.read_text(encoding="utf-8").strip()
        if not (pv == version or pv.startswith(version + ".")):
            problems.append(
                f".python-version {pv!r} does not match policy python.version {version!r}"
            )
    spec = str(pyproject.get("project", {}).get("requires-python", ""))
    m = re.fullmatch(r">=\s*(3\.\d+)(?:\.\d+)?", spec.strip())
    if not m or m.group(1) != version:
        problems.append(f"pyproject requires-python {spec!r} should be '>={version}'")
    return problems


def _check_tool_config_files(root: Path, policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    bandit_path = policy.get("bandit", {}).get("config")
    if bandit_path:
        f = root / bandit_path
        if not f.exists():
            problems.append(f"bandit.config {bandit_path} does not exist")
        else:
            try:
                data = load_yaml(f.read_text(encoding="utf-8"), bandit_path) or {}
            except PolicyError as exc:
                problems.append(str(exc))
                data = {}
            if data.get("skips"):
                problems.append(
                    f"{bandit_path}: global `skips` are forbidden; use inline `# nosec B### - reason`"
                )
    trivy_path = policy.get("trivy", {}).get("config_file")
    if trivy_path:
        f = root / trivy_path
        if not f.exists():
            problems.append(f"trivy.config_file {trivy_path} does not exist")
        else:
            try:
                data = load_yaml(f.read_text(encoding="utf-8"), trivy_path) or {}
            except PolicyError as exc:
                problems.append(str(exc))
                data = {}
            forbidden = {
                "severity",
                "exit-code",
                "ignore-unfixed",
                "ignorefile",
                "scanners",
                "skip-dirs",
                "skip-files",
                "ignore-policy",
                "vulnerability",
                "scan",
            }
            bad = sorted(forbidden & set(data))
            if bad:
                problems.append(
                    f"{trivy_path}: gate keys {bad} belong in the policy, not the tool config"
                )
    return problems


# --------------------------------------------------------------------------- workflows


USES_RE = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>[^\s#]+)")
INJECTION_RE = re.compile(
    r"\$\{\{\s*(?:github\.head_ref|github\.event\.(?:pull_request\.(?:title|body|head\.ref|head\.label|head\.repo\.\w+)"
    r"|issue\.(?:title|body)|comment\.body|review\.body|head_commit\.(?:message|author\.\w+)"
    r"|commits\S*|pages\S*|inputs\.[\w-]+)|inputs\.[\w-]+)\s*\}\}"
)


def _is_write(level: Any) -> bool:
    return isinstance(level, str) and level.lower() in {"write", "write-all"}


def _iter_workflow_files(root: Path) -> list[Path]:
    wf = root / ".github" / "workflows"
    if not wf.is_dir():
        return []
    return sorted(p for p in wf.iterdir() if p.suffix in {".yml", ".yaml"})


def _find_key(node: Any, key: str, expected: Any) -> bool:
    if isinstance(node, dict):
        return any(
            (k == key and v == expected) or _find_key(v, key, expected) for k, v in node.items()
        )
    if isinstance(node, list):
        return any(_find_key(v, key, expected) for v in node)
    return False


def check_pins(name: str, text: str) -> list[str]:
    """Every non-local ``uses:`` must be a 40-hex commit SHA (or a docker:// ref with @sha256:)."""
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = USES_RE.match(line)
        if not m:
            continue
        ref = m.group("ref").strip("'\"")
        if ref.startswith("./"):
            continue
        _, _, version = ref.partition("@")
        pinned = bool(SHA_RE.match(version)) or (ref.startswith("docker://") and "@sha256:" in ref)
        if not pinned:
            problems.append(
                f"{name}:{lineno}: `uses: {ref}` is not pinned to a full 40-hex commit SHA"
            )
    return problems


SECRET_REF_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")


def check_secret_scoping(name: str, doc: dict[str, Any], actions: dict[str, Any]) -> list[str]:
    """Secrets other than GITHUB_TOKEN: only in jobs with an `environment:`; never in secret-free workflows."""
    problems: list[str] = []
    secret_free = name in actions.get("secret_free_workflows", [])
    top = json.dumps({k: v for k, v in doc.items() if k != "jobs"}, default=str)
    if SECRET_REF_RE.search(top):
        problems.append(f"{name}: secrets referenced outside a job (top-level env/defaults)")
    for job_id, job in (doc.get("jobs") or {}).items():
        names = sorted(set(SECRET_REF_RE.findall(json.dumps(job, default=str))) - {"GITHUB_TOKEN"})
        if not names:
            continue
        if secret_free:
            problems.append(
                f"{name}: job {job_id} references secrets {names}; this workflow must be secret-free"
            )
        elif actions.get("secrets_require_environment") and not (job or {}).get("environment"):
            problems.append(
                f"{name}: job {job_id} references secrets {names} without an `environment:`"
            )
    return problems


def check_workflow(name: str, text: str, actions: dict[str, Any]) -> list[str]:  # noqa: PLR0912
    """Static checks on one workflow file's text against the ``actions`` policy section."""
    problems: list[str] = []
    if actions.get("pin_to_full_sha"):
        problems.extend(check_pins(name, text))
    try:
        doc = load_yaml(text, name)
    except PolicyError as exc:
        return [*problems, str(exc)]
    if not isinstance(doc, dict):
        return [*problems, f"{name}: workflow must be a mapping"]

    default = actions.get("default_permissions", {})
    if "permissions" not in doc:
        problems.append(f"{name}: top-level `permissions:` is missing (must be {default})")
    elif doc["permissions"] != default:
        problems.append(
            f"{name}: top-level permissions {doc['permissions']!r} must equal {default!r}"
        )

    triggers = doc.get("on", doc.get(True, {}))  # PyYAML parses bare `on` as boolean True
    names = (
        set(triggers)
        if isinstance(triggers, dict)
        else set(triggers if isinstance(triggers, list) else [triggers])
    )
    for forbidden in actions.get("forbid_triggers", []):
        if forbidden in names:
            problems.append(
                f"{name}: trigger `{forbidden}` is forbidden (runs untrusted fork code with secrets)"
            )

    allowed = actions.get("allowed_job_write_permissions", {})
    jobs = doc.get("jobs", {}) or {}
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        perms = job.get("permissions", {})
        if isinstance(perms, str):
            if _is_write(perms):
                problems.append(f"{name}: job {job_id} uses blanket `permissions: {perms}`")
            continue
        for perm, level in (perms or {}).items():
            if _is_write(level) and name not in allowed.get(perm, []):
                problems.append(
                    f"{name}: job {job_id} requests `{perm}: write`, not allowed by policy"
                )
        if job.get("secrets") == "inherit":
            problems.append(
                f"{name}: job {job_id} uses `secrets: inherit`; pass secrets explicitly"
            )
        for step in job.get("steps", []) or []:
            run = step.get("run") if isinstance(step, dict) else None
            if isinstance(run, str) and INJECTION_RE.search(run):
                problems.append(
                    f"{name}: job {job_id}: untrusted `${{{{ ... }}}}` expression interpolated into `run:`; pass via env"
                )
    problems.extend(check_secret_scoping(name, doc, actions))
    if actions.get("forbid_continue_on_error") and _find_key(doc, "continue-on-error", True):
        problems.append(f"{name}: `continue-on-error: true` is forbidden (gates must block)")
    return problems


def check_workflows(root: Path, policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if policy.get("actions", {}).get("pin_to_full_sha"):
        for action_file in sorted((root / ".github" / "actions").glob("*/action.y*ml")):
            rel = action_file.relative_to(root).as_posix()
            problems.extend(check_pins(rel, action_file.read_text(encoding="utf-8")))
    for path in _iter_workflow_files(root):
        problems.extend(
            check_workflow(path.name, path.read_text(encoding="utf-8"), policy.get("actions", {}))
        )
    return problems


# --------------------------------------------------------------------------- drift scan

_NOT_EXPR = r"(?!\$)"
DRIFT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"--(?:cov-)?fail[-_]under[= ]\s*[\"']?\d"),
        "hard-coded coverage threshold (use `quality_policy.py get coverage.fail_under`)",
    ),
    (
        re.compile(rf"--exit-code[= ]\s*[\"']?{_NOT_EXPR}\w"),
        "literal --exit-code (use `quality_policy.py args trivy-*`)",
    ),
    (
        re.compile(rf"--severity[= ]\s*[\"']?{_NOT_EXPR}[A-Za-z]"),
        "literal trivy --severity (use `quality_policy.py args trivy-*`)",
    ),
    (
        re.compile(r"--(?:severity|confidence)-level\b"),
        "literal bandit level (use `quality_policy.py args bandit`)",
    ),
    (
        re.compile(r"--ignore-vuln\b(?![= ]\s*\$)"),
        "literal --ignore-vuln (ignores live in the policy file with expiry)",
    ),
    (
        re.compile(r"--ignore-unfixed\b"),
        "literal --ignore-unfixed (use `quality_policy.py args trivy-*`)",
    ),
    (re.compile(r"--exit-zero\b"), "--exit-zero disables the bandit gate"),
    (
        re.compile(r"\b(?:bandit|pip-audit|trivy|ruff|mypy|pytest)\b.*\|\|\s*(?:true|:)\s*$"),
        "`|| true` swallows a gate failure",
    ),
]


def scan_text_for_drift(name: str, text: str) -> list[str]:
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pattern, message in DRIFT_PATTERNS:
            if pattern.search(line):
                problems.append(f"{name}:{lineno}: {message}")
    return problems


def check_drift(root: Path, policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    targets: list[Path] = []
    for rel in policy.get("drift_scan", {}).get("paths", []):
        p = root / rel
        if p.is_dir():
            targets.extend(
                sorted(
                    f
                    for f in p.rglob("*")
                    if f.is_file() and f.suffix in {".yml", ".yaml", ".py", ".sh", ".mk", ""}
                )
            )
        elif p.is_file():
            targets.append(p)
    for f in targets:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        problems.extend(scan_text_for_drift(str(f.relative_to(root)), text))
    return problems


# --------------------------------------------------------------------------- check driver


def run_check(
    root: Path, policy_path: Path | None = None, today: dt.date | None = None
) -> list[str]:
    """Run every check. Returns the list of problems (empty list == policy is healthy)."""
    today = today or dt.datetime.now(dt.UTC).date()
    policy_path = policy_path or (root / DEFAULT_POLICY)
    try:
        policy = load_policy(policy_path)
    except PolicyError as exc:
        return [str(exc)]
    problems = validate_schema(policy)
    if problems:
        return problems  # later checks assume a well-formed policy
    problems.extend(check_floors(policy))
    problems.extend(
        check_ignore_entries(
            policy["pip_audit"]["ignore"],
            "pip_audit.ignore",
            today,
            policy["pip_audit"]["ignore_max_days"],
        )
    )
    ignorefile = root / policy["trivy"]["ignorefile"]
    if ignorefile.exists():
        problems.extend(
            check_trivyignore(
                ignorefile.read_text(encoding="utf-8"),
                today,
                policy["trivy"]["ignore_max_days"],
                policy["trivy"]["ignorefile"],
            )
        )
    else:
        problems.append(
            f"trivy.ignorefile {policy['trivy']['ignorefile']} does not exist (keep it, even if empty)"
        )
    problems.extend(check_mirrors(root, policy))
    problems.extend(check_workflows(root, policy))
    problems.extend(check_drift(root, policy))
    return problems


# --------------------------------------------------------------------------- args


def build_args(policy: dict[str, Any], tool: str, today: dt.date | None = None) -> list[str]:
    """Flags for ``tool`` (no sub-command, no target; the caller appends the scan target)."""
    today = today or dt.datetime.now(dt.UTC).date()
    p = policy
    if tool == "bandit":
        b = p["bandit"]
        args = [
            "-r",
            *b["paths"],
            "-c",
            b["config"],
            "--severity-level",
            b["severity"],
            "--confidence-level",
            b["confidence"],
        ]
        return [*args, *([] if b["fail_on_findings"] else ["--exit-zero"])]
    if tool == "uv-export":
        scope = "--all-groups" if p["pip_audit"]["scope"] == "runtime+dev" else "--no-dev"
        return [
            "--quiet",
            "--frozen",
            "--no-hashes",
            "--no-emit-project",
            scope,
            "-o",
            p["pip_audit"]["requirements_file"],
        ]
    if tool == "pip-audit":
        pa = p["pip_audit"]
        args = [
            "-r",
            pa["requirements_file"],
            "--no-deps",
            "--disable-pip",
            "--strict",
            "--progress-spinner",
            "off",
        ]
        for ident in active_pip_audit_ignores(p, today):
            args += ["--ignore-vuln", ident]
        return args
    if tool == "k6":
        lt = p["load_test"]
        return [
            "-e",
            f"P95_MS={lt['p95_ms']}",
            "-e",
            f"ERROR_RATE_MAX={lt['error_rate_max']}",
            "--vus",
            str(lt["vus"]),
            "--duration",
            lt["duration"],
        ]
    return _trivy_args(p, tool)


def _trivy_args(p: dict[str, Any], tool: str) -> list[str]:
    t = p["trivy"]
    common = [
        "--config",
        t["config_file"],
        "--ignorefile",
        t["ignorefile"],
    ]
    skip = ["--skip-dirs", ",".join(t["skip_dirs"])]  # only meaningful for source-tree scans
    if tool == "trivy-fs":
        s = t["fs"]
        out = [
            *common,
            "--no-progress",
            *skip,
            "--scanners",
            ",".join(t["scanners_fs"]),
            "--severity",
            ",".join(s["severities"]),
            "--exit-code",
            str(s["exit_code"]),
        ]
        flags = (["--ignore-unfixed"] if s["ignore_unfixed"] else []) + (
            ["--include-dev-deps"] if s["include_dev_deps"] else []
        )
        return [*out, *flags]
    if tool in {"trivy-image", "trivy-image-report"}:
        s = t["image"]
        out = [
            *common,
            "--no-progress",
            "--scanners",
            ",".join(s["scanners"]),
            "--severity",
            ",".join(s["severities"]),
            "--platform",
            s["platform"],
        ]
        if tool == "trivy-image-report":
            return out  # non-blocking full report: no --exit-code, no --ignore-unfixed
        return [
            *out,
            "--exit-code",
            str(s["exit_code"]),
            *(["--ignore-unfixed"] if s["ignore_unfixed"] else []),
        ]
    if tool == "trivy-config":
        s = t["config"]
        return [
            *common,
            *skip,
            "--severity",
            ",".join(s["severities"]),
            "--exit-code",
            str(s["exit_code"]),
        ]
    raise PolicyError(f"unknown tool {tool!r}; choose from {', '.join(TOOLS)}")


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StockFlow quality policy tool")
    parser.add_argument(
        "--policy", type=Path, default=None, help=f"policy file (default: <root>/{DEFAULT_POLICY})"
    )
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="repository root (default: cwd)"
    )
    parser.add_argument(
        "--today", type=dt.date.fromisoformat, default=None, help="override today's date (tests)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="print one value by dotted key")
    g.add_argument("key")
    a = sub.add_parser("args", help="print command-line flags for a tool")
    a.add_argument("tool", choices=TOOLS)
    sub.add_parser("check", help="validate the policy and its mirrors")
    ns = parser.parse_args(argv)

    root: Path = ns.root.resolve()
    policy_path: Path = ns.policy or (root / DEFAULT_POLICY)
    try:
        if ns.cmd == "check":
            problems = run_check(root, policy_path, ns.today)
            if problems:
                print(f"quality policy check FAILED ({len(problems)} problem(s)):", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
                return 1
            print(f"quality policy OK ({policy_path})")
            return 0
        policy = load_policy(policy_path)
        if ns.cmd == "get":
            print(format_value(get_value(policy, ns.key)))
        else:
            print(shlex.join(build_args(policy, ns.tool, ns.today)))
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
