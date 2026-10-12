# Implementation status

Legend: [x] done and verified by a real command (result recorded), [ ] pending, **NOT VERIFIED** = built but
cannot be checked from this VM (needs GitHub or Render), **BLOCKED** = needs an external account or action.
Ticked only from real command output; nothing here is estimated.

Last synchronised: 2026-10-10 (repository has no commits and no remote).

## Verification summary (real runs on the dev VM, aarch64, Python 3.12.15, uv 0.12.24)

| Check | Command | Result |
|---|---|---|
| Tests + coverage | `uv run pytest -q --cov` | **1431 passed** in 69 s; branch coverage **100.00 %** (849 stmts, 104 branches; policy floor 85) |
| Marker split | `pytest --collect-only -m <marker>` | unit 441, integration 843 (concurrency 18 inside it), demo 117 (unit includes `tests/deploy/`, now with `test_preview.py`) |
| Lint / format / types | `make lint format-check typecheck` | ruff: All checks passed; 73 files formatted; mypy strict: no issues in 36 source files |
| Config validation | `make policy-check` | **FAILS** (2 problems): `pr-preview.yml: job publish requests packages: write, not allowed by policy`; `pr-preview.yml: job report requests pull-requests: write, not allowed by policy`. The policy (security) allows `packages: write` only in release.yml and `pull-requests` nowhere. This turns the CI `config-validation` job, and therefore `quality-gate`, red on the current tree |
| Security gates | `make security` | exit 0: policy-check, bandit "No issues identified", pip-audit "No known vulnerabilities found", trivy fs `uv.lock` 0 vulnerabilities, trivy config `Dockerfile` 0 misconfigurations |
| Container | `make docker-test` | exit 0: linux/amd64 image built, ran with `PORT=9123`, smoke test PASSED (version == baked sha), 8/8 image-guard checks, demo image-guard OK, SIGTERM stop in 1 s with exit 0 |
| Defect scenarios | `uv run python -m demo verify-all` | **13 passed, 0 failed, 0 skipped**; each: fails as expected at its real gate, signature matched, other gates stay green, exact reset, clean re-run passes; live tree afterwards `no scenario active`, no `DEMO-DEFECT` marker in `app/`, `pyproject.toml`, `uv.lock`, `Dockerfile` |

Not run by the architect in this pass: `make workflow-lint` (needs `actionlint`, installed by `make tools`),
the Trivy image gate on the saved tarball (`make docker-scan`; earlier devops reports exist under `reports/`
but were not re-run here), and `make perf`.

## Phase 0 - Architecture and skeleton (architect)
- [x] Architecture contract: docs/ARCHITECTURE.md (re-synchronised with the implementation on 2026-10-10)
- [x] pyproject.toml (deps, ruff, mypy strict, pytest, coverage fail_under 85 + branch)
- [x] `.python-version` (3.12) and committed `uv.lock` (`uv lock --check` passes)
- [x] Directory skeleton; CLAUDE.md
- [x] `pyyaml` is in the dev group (verified in `pyproject.toml`; runtime image has only the six runtime deps)
- [ ] Remove leftover `.gitkeep` files in directories that now have content (owners: devops `.github/workflows`, `scripts/deploy`; qa `tests`, `load-tests`; frontend `app/static`, `app/templates`)

## Phase 1 - Domain, repositories, services (backend)
- [x] app/models (domain, schemas, errors)
- [x] app/repository (base, memory, sqlite, seed, factory); 100 % covered, contract suite runs on both backends
- [x] app/services (pricing, inventory, orders, products, metrics); pricing golden vectors asserted
- [x] app/config.py
- Deviations from the first contract, now documented in ARCHITECTURE.md: `low_stock=false` does not filter;
  `tax_rate` is not in the template context.

## Phase 2 - API (backend)
- [x] create_app, routers, error handlers per ARCHITECTURE section 5 (API tests cover every error code)
- [x] /health, /ready, /api/version, /api/metrics
- [x] `python -m app` binds `$HOST:$PORT` (0.0.0.0), one worker; exercised inside the container by `make docker-test`

## Phase 3 - Dashboard (frontend)
- [x] templates + precompiled CSS + JS served; `tests/api/test_web.py` passes; dashboard check passes in the container smoke test
- [ ] Visual / accessibility review in a real browser: not performed by the architect (NOT VERIFIED)
- [ ] `tax_rate` context key so the order preview follows `TAX_RATE` (backend, optional)

## Phase 4 - Tests (qa)
- [x] unit, service, repository contract, API, invariants, concurrency tests (1431 pass)
- [x] coverage 100.00 % branch, floor 85 enforced by `make test-coverage` and mirrored in the policy
- [x] scripts/smoke_test.py (exercised against the container)
- [x] load-tests (`performance.js`, `smoke.js`); `slow_api` verification drives k6 successfully
- [ ] Load test against the real Render service: BLOCKED (no service)

## Phase 5 - Quality policy and security (security)
- [x] config/quality-policy.yaml + scripts/quality_policy.py (`check` passes)
- [x] bandit/trivy config, `.trivyignore`; `trivy.image.ignore_unfixed: true` justified in docs/SECURITY.md 7.2
- [x] docs/SECURITY.md
- [x] `make security` green on the clean baseline (see summary)

## Phase 6 - Container and Make (devops)
- [x] Dockerfile (amd64, non-root, allow-list .dockerignore), compose.yaml, render.yaml
- [x] Makefile targets (see CLAUDE.md)
- [x] `make docker-test` verified (see summary)
- [ ] `make compose-up` and `make docker-scan` re-run by the architect: not done (devops reports only)

## Phase 7 - CI/CD (devops)
- [x] Workflows written: ci.yml (aggregate `quality-gate`), release.yml (`ci`, `build-scan`, `publish`, `deploy`), rollback.yml, demo-unsafe-deploy.yml, perf.yml; local composite actions; dependabot
- [x] Policy `check` validates action pinning, permissions and drift scan over these files
- [x] pr-preview.yml written (jobs decide, gate, build-scan, publish, preview, cleanup, report) with `scripts/deploy/preview_lib.py`, `preview_tools.py` and `tests/deploy/test_preview.py` (44 test functions) passing locally against a fake Render/GitHub
- [ ] **Policy blocks pr-preview.yml**: security must extend `actions.allowed_job_write_permissions` (`packages: [release.yml, pr-preview.yml]`, `pull-requests: [pr-preview.yml]`) in config/quality-policy.yaml and SECURITY.md; until then `make policy-check` and two QA tests fail
- [ ] pr-preview.yml executing on GitHub/Render (label opt-in, quality-gate wait, digest-based `POST /v1/services/{id}/preview`, redeploy, cleanup, sticky comment): **NOT VERIFIED / BLOCKED**
- [ ] Render points unconfirmed for previews (**NOT VERIFIED**): free-plan base -> free preview (`PREVIEW_PLAN` left unset), `imagePath` accepting `repo@sha256:digest`, preview URL shape `https://<name>.onrender.com`, inherited env vars (`DEPLOY_ENVIRONMENT=production`), redeploy by `imageUrl` for an existing preview
- [ ] Environment `preview` created with `RENDER_API_KEY` / `RENDER_SERVICE_ID` and deployment-branch/reviewer rules: **BLOCKED** (manual repository setting)
- [ ] actionlint run over the workflows (`make workflow-lint`): not run in this pass
- [ ] `ci.yml` executing on GitHub: **NOT VERIFIED** (no remote)
- [x] release.yml written so that a push to main REQUIRES a verified deployment: unconfigured hook/base URL fails `deploy` (publish to GHCR and `release-record.json` still happen first); manual `require_deploy` defaults to true; `release_tools.py verify-deployment` compares release-record digest, the imgURL digest and the live `/api/version`; `deployed: true` / "DEPLOYED and verified" only when all passed. Unit-tested helper logic (`tests/deploy/`) passes locally
- [ ] release.yml actually executing (GHCR push, digest capture, Render deploy, Render API image.ref check, smoke, verify-deployment, record): **NOT VERIFIED / BLOCKED** (needs GitHub, GHCR package visibility, `RENDER_DEPLOY_HOOK_URL`, `STOCKFLOW_BASE_URL`, optional `RENDER_API_KEY`/`RENDER_SERVICE_ID`)
- [ ] rollback.yml, demo-unsafe-deploy.yml, perf.yml on GitHub: **NOT VERIFIED / BLOCKED**
- [ ] Branch protection requiring only `quality-gate`: **BLOCKED** (manual repository setting)
- [x] `demo-fixtures.yml` (scheduled `--validate-fixtures`) written and actionlint-clean; the scheduled run on GitHub is **NOT VERIFIED**
- [ ] SHA pins verified against the GitHub API by the architect: not done (devops/security responsibility)

## Phase 8 - Defect injection (demo-engineer)
- [x] Engine: list / show / inject / status / reset / verify / verify-all / validate-fixtures / image-guard; ops replace_text, replace_regex, create_file, replace_file; `--force`; `--allow-runtime-failures`
- [x] 13 scenarios (`clean`, `bad_lint`, `bad_format`, `bad_typecheck`, `bad_unit_test`, `bad_integration_test`, `bad_coverage`, `bad_security`, `bad_dependency`, `bad_container`, `slow_api`, `unhealthy_health`, `bad_deployment`)
- [x] tests/demo (111 tests); baseline-clean guard passes
- [x] `demo verify-all`: **13/13 PASS, 0 skipped** (run by the architect, 2026-10-10)
- [ ] `python -m demo validate-fixtures` against live advisory databases: not re-run in this pass
- [x] Makefile `demo` / `demo-reset` forward `DEMO_ARGS` (present in the Makefile at last check; not exercised by the architect)

## Phase 9 - Documentation (documentation)
- [ ] README.md is still the architect's 3-line placeholder
- [ ] docs/QUALITY_GATES.md, docs/DEMO_RUNBOOK.md (35-45 min timeline), docs/SETUP_GITHUB_RENDER.md, docs/TROUBLESHOOTING.md: not present at last check (a `docs/images/` directory has appeared; work in progress)
- [ ] every documented command executed and verified

## Phase 10 - Integration and release readiness
- [ ] Clean baseline passes every gate: lint, format, types, coverage and `make security` pass on their own, but `make policy-check` and two policy tests FAIL because of pr-preview.yml permissions (see summary); re-tick after security updates the policy
- [x] `make docker-test` green on the linux/amd64 image
- [x] `demo-verify-all` green; `demo-reset` leaves a clean tree (`no scenario active`, no markers)
- [ ] `make quality` as a single command: its components passed individually; the single-command run was
      performed inside `demo verify-all` (`clean` scenario, PASS in 435 s), not separately by the architect
- [ ] Trivy image gate on the final tarball per policy: earlier devops run only, not re-run here
- [ ] Workflow review with actionlint and pin verification: pending
- [ ] First real release to GitHub/Render and a rollback rehearsal: **BLOCKED** (accounts, secrets, remote)
- [ ] Open questions resolved by the project owner (public GHCR package, demo-unsafe environment reviewers, Render plan)
