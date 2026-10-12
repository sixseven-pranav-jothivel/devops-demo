# StockFlow defect-injection engine

Deterministic, reversible, tool-verified defects for the CI/CD demo. Source of truth:
`demo/scenarios.yaml`. Engine: `demo/engine.py`. Contract: `docs/ARCHITECTURE.md` section 11.

```
python -m demo list                       # id, category, gate, stage, deployable?
python -m demo show bad_dependency        # files, signature, reproduce commands, remediation
python -m demo inject bad_lint            # apply (switching restores the baseline first)
python -m demo status                     # active scenario + per-file state (injected/baseline/MODIFIED)
python -m demo reset                      # restore the exact clean files
python -m demo verify bad_lint            # inject -> real gate FAILS as declared -> reset -> gate PASSES
python -m demo verify-all                 # every scenario, summary table, reports/demo-verify.json
python -m demo validate-fixtures          # = python demo/inject.py --validate-fixtures
python -m demo image-guard stockflow:x    # a built image must contain no demo/ material
```

`demo/inject.py <id>`, `demo/reset.py` and `demo/verify.py [ids]` are thin script entry points for the same
code. Make targets (devops): `demo-list`, `demo SCENARIO=<id> [DEMO_ARGS=...]`, `demo-reset`,
`demo-verify SCENARIO=<id>`, `demo-verify-all`.

## Scenarios

| id | category | file(s) | real gate that fails | stage | deploy? |
|---|---|---|---|---|---|
| `clean` | baseline | none | none, everything passes | pr-ci | yes |
| `bad_lint` | lint | `app/services/orders.py` | `ruff check` (F401) | pr-ci | no |
| `bad_format` | format | `app/services/pricing.py` | `ruff format --check` | pr-ci | no |
| `bad_typecheck` | types | `app/services/products.py` | `mypy` (return-value) | pr-ci | no |
| `bad_unit_test` | logic | `app/services/pricing.py` | `pytest tests/unit` | pr-ci | no |
| `bad_integration_test` | logic | `app/services/orders.py` | `pytest tests/services tests/api` | pr-ci | no |
| `bad_coverage` | coverage | `app/services/legacy_import.py` (new) | `pytest --cov` (fail_under) | pr-ci | no |
| `bad_security` | security | `app/services/metrics.py` | `bandit` (B602) | pr-ci | no |
| `bad_dependency` | dependency | `pyproject.toml`, `uv.lock` | `pip-audit` and `trivy fs` | pr-ci | no |
| `bad_container` | container | `Dockerfile` | `trivy image` | image-build | no |
| `slow_api` | runtime | `app/api/products.py` | `k6` p95 threshold | post-deploy | yes* |
| `unhealthy_health` | runtime | `app/api/health.py` | `scripts/smoke_test.py` | post-deploy | yes* |
| `bad_deployment` | runtime | `app/main.py` | app exits at startup | post-deploy | yes* |

\* The runtime scenarios pass every PR gate on purpose (that is the lesson: only production signals catch
them). They are inert until the operator sets the env keys described below.

Each scenario declares: `name`, `description`, affected `files`, the `expected` failing gate with its
command and `output_contains` signature, `reproduce` commands, `remediation`, `deployment_allowed`,
optional `requires` (docker, trivy, k6, network, dockerfile) and `must_still_pass` (the other gates that
must stay green while the defect is active, proving the defect is isolated).

## Safety properties

- **Real edits, real tools.** Patch ops: `replace_text` (old text must occur exactly once), `replace_regex`
  (pattern must match exactly once), `create_file` / `replace_file` (inline or fixture `source` with a
  `sha256` pin). Every inserted hunk carries `DEMO-DEFECT:<id>`; the baseline has none
  (`tests/demo/test_baseline_clean.py`).
- **Idempotent.** Injecting the active scenario is a no-op. Injecting another one resets first.
- **Transactional.** Backups and a write-ahead `state.json` (`status: applying`) are written before any file
  changes; files are replaced atomically; a failure rolls back; a crash is healed by `reset`.
- **Reset is exact.** Files are restored from `.demo-state/backup/<id>/` after a sha256 check of the backup,
  then the restored file is re-hashed against its recorded baseline hash. `verify` additionally compares a
  git-independent tree digest before inject and after reset. Files created by a scenario are deleted.
- **Refuses to clobber your work.** If a file touched by the active scenario now has a hash that is neither
  the injected nor the original content, `reset` and `inject <other>` stop with exit code 2 and list the
  files. `--force` overrides it; your edited copy is kept under `.demo-state/discarded/<timestamp>/`.
  `inject` also refuses when target files already contain `DEMO-DEFECT:` markers but no state exists (the
  tree is not at baseline), and `create_file` refuses to overwrite a different existing file.
- **Never weakens scanning.** No scenario touches `config/`, `.trivyignore`, workflows, the Makefile, or any
  `[tool.*]` section; none adds an ignore. A test enforces this.
- **Runtime faults are two-key, env-only.** `slow_api`, `unhealthy_health` and `bad_deployment` inject code
  that is active only if the process environment has BOTH `STOCKFLOW_DEMO_FAULT=<scenario id>` and
  `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true`. They never read request headers, query parameters, cookies
  or bodies. The engine itself refuses to inject them unless `demo.allow_runtime_failures: true` in
  `scenarios.yaml` or `--allow-runtime-failures` is passed (default: off). Merging the patch alone changes
  nothing in production; demonstrating on Render means setting both variables on the service explicitly.
- **Fixtures are isolated.** `demo/fixtures/` is never imported or installed; the allow-list `.dockerignore`
  keeps all of `demo/` out of the image (`python -m demo image-guard <image>` checks a built image).

## bad_dependency in the real flow

The defect is a direct runtime dependency `python-multipart==0.0.6` added to `pyproject.toml` and a
consistent `uv.lock` (three hunks, generated by `uv lock` and checked with `uv lock --check`). That version
has CVE-2024-24762 / GHSA-2jv5-9r88-3w3p (HIGH, fixed in 0.0.7) and later advisories; both `pip-audit` (on
`uv export` of the lock) and `trivy fs` report it. Because the real lock is patched, **CI needs no special
step**: the normal `security` job (export lock -> pip-audit, trivy fs) fails. The package is also installed
into the image by `uv sync --frozen --no-dev`, so the image scan would report it too. Reset restores both
files byte for byte.

Limitation: the advisory lives in external databases (OSV/PyPI for pip-audit, Trivy DB). If it is withdrawn
or re-scored the scenario would silently stop failing. `python demo/inject.py --validate-fixtures` re-checks
it against the live databases and exits non-zero naming the stale fixture; run it before a demo and in CI
(schedule). Exit code 3 means "could not verify" (offline), which is not a pass.

## bad_container and the Dockerfile contract

`bad_container` rewrites the one base-stage line of the Dockerfile, `FROM python:... AS base` (the line
devops reserved for it), to `python:3.12.0-slim-bookworm@sha256:19a6...` (2023 Debian; 92 fixable
HIGH/CRITICAL findings when checked). The patch is `replace_regex ^FROM python:\S+ AS base$`, which must
match exactly once, so a Dockerfile refactor fails loudly instead of silently. The clean Dockerfile is never
edited otherwise, and the `trivy-image` policy (`ignore_unfixed: true`) is untouched: the findings block
because they have fixes. The image still builds, so the failure is at the Trivy image gate, as it would be
after an unattended base image drift.

## Verification (`demo/verify.py`)

For each scenario: confirm requirements (docker/trivy/k6/network/Dockerfile, otherwise `SKIPPED` with the
reason, never reported as passed) -> inject -> run the declared command, require a non-zero exit and every
`output_contains` string -> run each `must_still_pass` gate -> reset -> require identical tree digest ->
re-run the command and require success. Runtime scenarios additionally start the app on a free port and:
run the gate with both env keys (must fail); start it with no keys, only one key, and with forged
headers/query parameters (must behave normally); re-run after reset (must pass).

By default verification runs in a throw-away copy of the repository that reuses the live `.venv`
(`UV_NO_SYNC=1`), so other people working in the live tree never see an injected defect. Use `--in-place` to
verify the live tree. Results are written to `reports/demo-verify.json`.

## Presenting a scenario

```
python -m demo inject bad_lint && git switch -c demo/bad-lint && git commit -am "demo: bad_lint"   # your commit
# push, watch CI fail at the declared gate, then fix by reset:
python -m demo reset
```
`.demo-state/` is git-ignored: the state file and backups never leave your machine, so a committed defect
is fixed by reverting the commit (or `reset` while the same working tree is still active). Never merge a
`DEMO-DEFECT:` commit to `main`; the baseline guard test and the image guard exist to catch that.
