"""`python -m app` starts exactly one uvicorn worker on the configured address."""

from __future__ import annotations

import runpy
import sys
from typing import Any

import pytest
import uvicorn

from app import __main__ as entrypoint
from app.main import app

pytestmark = pytest.mark.unit


def test_main_runs_uvicorn_with_one_worker_on_the_configured_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("HOST", "127.0.0.1")

    entrypoint.main()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (app,)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 10000
    assert kwargs["workers"] == 1  # the repository is in-process memory: never more than one


def test_main_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *_a, **kw: calls.append(kw))
    entrypoint.main()
    assert (calls[0]["host"], calls[0]["port"], calls[0]["workers"]) == ("0.0.0.0", 8000, 1)


def test_running_the_module_as_a_script_calls_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **kw: calls.append(kw))
    monkeypatch.delitem(sys.modules, "app.__main__")  # run it fresh, as `python -m app` would
    runpy.run_module("app.__main__", run_name="__main__")
    assert len(calls) == 1
    assert calls[0]["workers"] == 1
