"""Fixtures for the engine tests: every mutation happens in a tmp copy of the repo."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

from demo.engine import MARKER_PREFIX, Engine  # noqa: E402

COPIED_DIRS = ("app", "demo")
COPIED_FILES = ("pyproject.toml", "uv.lock", "Dockerfile", ".dockerignore", ".python-version")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.demo-tmp", ".demo-state")


def live_state_active() -> bool:
    return (REPO / ".demo-state" / "state.json").exists()


def live_tree_has_committed_defect() -> bool:
    """A `demo/<scenario>` branch commits its defect, so the live tree is not a baseline to copy.

    Elsewhere a committed marker is caught by test_baseline_clean.py, which skips this fixture.
    """
    files = [p for p in (REPO / "app").rglob("*.py") if "__pycache__" not in p.parts]
    files += [REPO / name for name in COPIED_FILES if (REPO / name).is_file()]
    return any(MARKER_PREFIX in p.read_text("utf-8", errors="replace") for p in files)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A private copy of the files the scenarios touch (never the live tree)."""
    if live_state_active():
        pytest.skip("a demo scenario is active in the live tree; the copy would not be a baseline")
    if live_tree_has_committed_defect():
        pytest.skip("a demo defect is committed in the live tree; the copy would not be a baseline")
    root = tmp_path / "repo"
    root.mkdir()
    for name in COPIED_DIRS:
        shutil.copytree(REPO / name, root / name, ignore=IGNORE)
    for name in COPIED_FILES:
        if (REPO / name).is_file():
            shutil.copy(REPO / name, root / name)
    return root


@pytest.fixture
def engine(repo: Path) -> Engine:
    return Engine(repo)
