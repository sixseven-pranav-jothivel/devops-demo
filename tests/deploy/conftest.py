"""Make ``scripts/deploy`` importable (the helpers are plain scripts, not an installed package)."""

from __future__ import annotations

import sys
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "deploy"
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))
