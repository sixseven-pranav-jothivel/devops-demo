"""Script entry point: ``python demo/reset.py [--force]`` (same as ``python -m demo reset``)."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.cli import main

if __name__ == "__main__":
    sys.exit(main(["reset", *sys.argv[1:]]))
