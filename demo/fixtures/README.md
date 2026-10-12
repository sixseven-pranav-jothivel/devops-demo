# Demo fixtures (insecure on purpose)

Everything under `demo/fixtures/` is raw material for defect scenarios. It is never imported, never
installed, never packaged: the allow-list `.dockerignore` (`*` then `!app`, `!pyproject.toml`, `!uv.lock`)
keeps `demo/` out of every Docker build context, and `python -m demo image-guard <image>` proves it.

Rules for adding a fixture:

- Python fixtures use a non-`.py` suffix (`legacy_import.py.txt`) so ruff, mypy and bandit do not lint them.
- Never name a fixture `requirements.txt`, `uv.lock`, `poetry.lock`, ... : Trivy's analyzers match those exact
  names and would flag the clean tree. The vulnerable pin lives in `vulnerable-pin.txt`.
- No secret-shaped strings (Trivy's secret scanner runs over the repository).
- Pin `create_file` / `replace_file` sources with `sha256:` in `scenarios.yaml`; the engine refuses a
  fixture whose hash changed.
