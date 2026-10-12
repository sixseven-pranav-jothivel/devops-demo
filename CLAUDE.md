# StockFlow - project guide for agents

Read `docs/ARCHITECTURE.md` first: it is the contract (API, models, repository interface, service
signatures, pipeline, defect scenarios, ownership) and is kept in sync with the implemented code. This file
is the short version.

## What this is
StockFlow is a small FastAPI inventory and order management app. Its real purpose is to demonstrate a
complete CI/CD and DevOps lifecycle with controlled, reversible defect injection: GitHub Actions -> quality
gates -> Docker (linux/amd64) -> Trivy -> GHCR -> Render (image-backed web service, deployed by digest) ->
smoke test -> deployment record -> manual rollback by digest. k6 for load tests.

## Architecture decisions
- Python 3.12+ (`.python-version`), FastAPI, Pydantic v2, Jinja2 server-rendered dashboard, precompiled
  committed CSS (no CDN, no Node pipeline), uv with committed `uv.lock`. Runtime deps: fastapi, uvicorn,
  jinja2, pydantic, pydantic-settings, sqlalchemy. `pyyaml` is dev-only (demo engine, policy script).
- Layers: `app/api` (routers, error handlers) -> `app/services` (single-purpose modules: `pricing.py`,
  `inventory.py`, `orders.py`, `products.py`, `metrics.py`) -> `app/repository` (Protocol + in-memory +
  SQLite). Domain models in `app/models`.
- Production uses the in-memory seeded repository, single worker; data is NON-persistent (resets on
  restart/deploy) and the UI/docs must say so. Tests use isolated SQLite DBs. The Repository Protocol allows
  a managed DB later.
- Money is `Decimal`, `ROUND_HALF_UP` to cents. Discount tiers by subtotal (>=100.00: 5%, >=500.00: 10%);
  tax 8% (`TAX_RATE`) on the discounted subtotal. Order creation + stock decrement are one unit of work.
- Config from env only (`app/config.py`): `APP_VERSION`, `GIT_SHA`, `DEPLOY_ENVIRONMENT`, `PORT`, ...
- Defects are injected by `python -m demo` from `demo/scenarios.yaml` via reversible text patches with hash
  checks (ops: `replace_text`, `replace_regex`, `create_file`, `replace_file`). Baseline code has no fault
  hooks. Runtime scenarios (`slow_api`, `unhealthy_health`, `bad_deployment`) are off by default: the engine
  needs `--allow-runtime-failures` (or `demo.allow_runtime_failures: true`) AND the injected code needs both
  `STOCKFLOW_DEMO_FAULT=<id>` and `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true` in the process env. They never
  read request data.
- One quality policy: `config/quality-policy.yaml` (coverage >= 85 with branch coverage, bandit
  medium/medium, trivy fs strict, trivy image `ignore_unfixed: true` per docs/SECURITY.md 7.2, ...).
  Read it with `uv run python scripts/quality_policy.py get|args|check`.
- Workflows (`.github/workflows/`): `ci.yml` (jobs roll up into the required `quality-gate`), `release.yml`
  (`ci` -> `build-scan` -> `publish` -> `deploy`), `rollback.yml`, `demo-unsafe-deploy.yml`, `perf.yml`.
  A push to main REQUIRES a verified deployment: missing `RENDER_DEPLOY_HOOK_URL` / `vars.STOCKFLOW_BASE_URL`
  fails the `deploy` job (the image is still published to GHCR first). Manual runs default `require_deploy: true`.
  `deployment-record.json` says `deployed: true` only after Render, the smoke test and `verify-deployment` all passed.

## Commands (Makefile is owned by devops; uv lives in ~/.local/bin)
```
export PATH=$HOME/.local/bin:$PATH
make sync                    # uv sync --frozen
make run                     # python -m app on $PORT (default 8000)
make lint                    # ruff check .
make format-check            # ruff format --check .
make format                  # apply formatting (not a gate)
make typecheck               # mypy (strict; app + demo)
make test                    # pytest (all)
make test-unit               # pytest -m "not integration"
make test-integration        # pytest -m integration
make test-coverage           # pytest --cov, branch, fails under policy coverage.fail_under (85)
make security                # policy-check + bandit + pip-audit + trivy fs + trivy config
make quality                 # lint, format-check, typecheck, test-coverage, security
make policy-check lock-check workflow-lint   # config validation (workflow-lint needs actionlint: make tools)
make docker-build            # linux/amd64 image (IMAGE=stockflow:local)
make docker-run              # run the image in the foreground on HOST_PORT
make docker-test             # build + run + smoke test + image guard + graceful stop
make docker-scan             # docker save + Trivy image gate (needs the image built)
make compose-up / compose-down
make smoke BASE_URL=... [EXPECT_SHA=...]
make perf BASE_URL=...       # k6 against a running instance (writes data)
make demo-list               # list defect scenarios
make demo SCENARIO=<id> [DEMO_ARGS=--allow-runtime-failures]   # inject a defect (the flag is required for runtime scenarios)
make demo-reset              # restore baseline
make demo-verify SCENARIO=<id>
make demo-verify-all
```
Docker needs `sudo` on the dev VM (the Makefile detects it). Host is aarch64; the production image is
linux/amd64 (emulated locally, slow).

## File ownership (no overlap; see ARCHITECTURE.md section 13)
- architect: docs/ARCHITECTURE.md, docs/IMPLEMENTATION_STATUS.md, CLAUDE.md, pyproject.toml (except pytest/coverage sections), uv.lock, .gitignore, .python-version
- backend: `app/` except templates/ and static/
- frontend: `app/templates/`, `app/static/`
- qa: `tests/` (except tests/demo, tests/deploy), `load-tests/`, `scripts/smoke_test.py`, pytest/coverage sections of pyproject.toml
- demo-engineer: `demo/`, `tests/demo/`
- devops: Dockerfile, .dockerignore, compose.yaml, render.yaml, Makefile, `.github/`, `scripts/deploy/`, `tests/deploy/`
- security: `docs/SECURITY.md`, `config/`, `.trivyignore`, `scripts/quality_policy.py`
- documentation: README.md, docs/QUALITY_GATES.md, DEMO_RUNBOOK.md, SETUP_GITHUB_RENDER.md, TROUBLESHOOTING.md
Edit only files you own; ask for other changes in your report. New dependencies: request from architect.

## Rules
- Never fabricate command output, scan results or test results. Run the real tool and report the outcome.
  Nothing that depends on GitHub or Render is verified until it has run there; say "NOT VERIFIED".
- The clean baseline must always pass every gate. Never commit a defect; `make demo-reset` must leave a clean tree.
- Never weaken scanners, thresholds or policy to make something pass; fix the code. Ignores need a
  justification and an expiry.
- No secrets in the repo. Do not touch external accounts or create cloud resources.
- Pin third-party GitHub Actions to full commit SHAs verified via the GitHub API.
- Do not commit to git unless explicitly asked.
