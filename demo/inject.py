"""Script entry point: ``python demo/inject.py <scenario>`` or ``--validate-fixtures``.

Equivalent to ``python -m demo inject <scenario>`` / ``python -m demo validate-fixtures``.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.cli import main


def entry(argv: list[str]) -> int:
    if "--validate-fixtures" in argv:
        rest = [a for a in argv if a != "--validate-fixtures"]
        return main(["validate-fixtures", *rest])
    if "--list" in argv:
        return main(["list"])
    return main(["inject", *argv])


if __name__ == "__main__":
    sys.exit(entry(sys.argv[1:]))
