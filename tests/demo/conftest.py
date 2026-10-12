"""Fixtures for the engine tests: every mutation happens in a tmp copy of the repo."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

from demo.engine import Engine  # noqa: E402

COPIED_DIRS = ("app", "demo")
COPIED_FILES = ("pyproject.toml", "uv.lock", "Dockerfile", ".dockerignore", ".python-version")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.demo-tmp", ".demo-state")


def live_state_active() -> bool:
    return (REPO / ".demo-state" / "state.json").exists()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A private copy of the files the scenarios touch (never the live tree)."""
    if live_state_active():
        pytest.skip("a demo scenario is active in the live tree; the copy would not be a baseline")
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
