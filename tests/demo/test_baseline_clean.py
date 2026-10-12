"""The committed baseline must carry no defect markers (`make demo-reset` leaves a clean tree)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from demo.engine import MARKER_PREFIX, Engine

REPO = Path(__file__).resolve().parents[2]
SCANNED = ("app", "pyproject.toml", "uv.lock", "Dockerfile", "compose.yaml", "render.yaml")

pytestmark = pytest.mark.demo


def _skip_if_defects_are_expected() -> None:
    """Defects are legitimate with an active scenario, and on `demo/*` branches.

    CI sets GITHUB_HEAD_REF (pull requests) / GITHUB_REF_NAME (pushes) itself, so branches named
    `demo/<scenario>` fail only at their declared gate. Everywhere else (main, release, feature
    branches, local clean tree) a marker fails this test.
    """
    if (REPO / ".demo-state" / "state.json").exists():
        pytest.skip("a demo scenario is currently injected in the live tree")
    ref = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or ""
    if ref.startswith("demo/") or os.environ.get("STOCKFLOW_DEMO_ALLOW_DEFECTS") == "1":
        pytest.skip(f"defects are allowed on demo branches (ref '{ref}')")


def _markers(root: Path) -> list[str]:
    hits: list[str] = []
    for entry in SCANNED:
        target = root / entry
        files = (
            [target] if target.is_file() else sorted(target.rglob("*")) if target.is_dir() else []
        )
        for path in files:
            if path.is_file() and "__pycache__" not in path.parts:
                text = path.read_text("utf-8", errors="replace")
                if MARKER_PREFIX in text:
                    hits.append(path.relative_to(root).as_posix())
    return hits


def test_live_baseline_has_no_demo_defect_markers() -> None:
    _skip_if_defects_are_expected()
    assert _markers(REPO) == []


def test_every_scenario_file_is_clean_in_the_live_tree() -> None:
    _skip_if_defects_are_expected()
    engine = Engine(REPO)
    status = engine.status()
    assert status["active"] is None
    assert status["stray_markers"] == []


def test_baseline_guard_detects_a_planted_marker(repo: Path) -> None:
    (repo / "app" / "services" / "pricing.py").write_text(
        "# DEMO-DEFECT:bad_unit_test\n", encoding="utf-8"
    )
    assert _markers(repo) == ["app/services/pricing.py"]
