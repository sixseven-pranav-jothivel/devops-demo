# Implementation status

Legend: [x] done and verified by a real command (result recorded), [ ] pending, **NOT VERIFIED** = built but
cannot be checked from this VM (needs GitHub or Render), **BLOCKED** = needs an external account or action.
Ticked only from real command output; nothing here is estimated.

Last synchronised: 2026-10-12. The local clone has no commits, but the pipeline has run on GitHub and Render (see Phase 7).

## Verification summary (real runs on the dev VM, aarch64, Python 3.12.15, uv 0.12.24)

| Check | Command | Result |
|---|---|---|
| Tests + coverage | `uv run pytest -q --cov` | **1431 passed** (0 failed) in 48 s; branch coverage **100.00 %** (849 stmts, 104 branches; policy floor 85) |
| Marker split | `pytest --collect-only -m <marker>` | unit 469, integration 845 (concurrency 18 inside it), demo 117; total 1431 |
| Lint / format / types | `make lint format-check typecheck` | ruff: All checks passed; 78 files formatted; mypy strict: no issues in 36 source files |
| Config validation | `make policy-check`, `actionlint .github/workflows/*.yml` | quality policy OK (it now allows the `pr-preview.yml` write permissions); actionlint exit 0 |
| Security gates | `make security` | exit 0: policy-check, bandit "No issues identified", pip-audit "No known vulnerabilities found", trivy fs `uv.lock` 0 vulnerabilities, trivy config `Dockerfile` 0 misconfigurations |
| Container | `make docker-test` | exit 0: linux/amd64 image built, ran with `PORT=9123`, smoke test PASSED (version == baked sha), 8/8 image-guard checks, demo image-guard OK, SIGTERM stop in 1 s with exit 0 |
| Defect scenarios | `uv run python -m demo verify-all` | **13 passed, 0 failed, 0 skipped**; each: fails as expected at its real gate, signature matched, other gates stay green, exact reset, clean re-run passes; live tree afterwards `no scenario active`, no `DEMO-DEFECT` marker in `app/`, `pyproject.toml`, `uv.lock`, `Dockerfile` |

Not run by the architect in this pass: the Trivy image gate on the saved tarball (`make docker-scan`; earlier devops reports exist under `reports/`
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
- [ ] Load test against the real Render service (`perf.yml`): **NOT VERIFIED** (a service exists now, but the workflow has not been run)

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
- [x] Policy now allows the `pr-preview.yml` write permissions (`packages`, `pull-requests`); `make policy-check` passes (re-run by the architect)
- [x] actionlint over the workflows: exit 0 (re-run by the architect)
- Real GitHub + Render evidence below was reported by the orchestrator (2026-10-12); the architect did not run it:
- [x] `ci.yml` on GitHub: green, all jobs
- [x] `release.yml` on GitHub: green: build-scan, publish (GHCR), deploy via hook, Render deploy live, deployed digest verified through Render's deploy history, live `/api/version.git_sha` == pushed commit
- [x] Render API polling in `release.yml` (`RENDER_API_KEY` + `RENDER_SERVICE_ID` in environment `production`): saw `update_in_progress` -> `live`, image digest matched (run 38206976235)
- [x] `pr-preview.yml` happy path: PR #3 with label `preview`, run 38209130008 passed decide, gate, build-scan, publish, preview, report; `stockflow-pr-3` created (parent = base) on the Free plan by `@sha256` `imagePath`; URL `https://stockflow-pr-3.onrender.com`; served version `0.1.0-pr3` and the PR head sha; inherited `DEPLOY_ENVIRONMENT=production` (cosmetic)
- [x] `pr-preview.yml` cleanup on PR close: run 38209469656 deleted the service (verified via the Render API); sticky comment "Preview deleted"
- [x] Render points confirmed: Free base -> Free preview, digest `imagePath`, URL shape, inherited env vars (see ARCHITECTURE 10.1)
- [x] GitHub configuration verified: branch protection requiring `quality-gate`; environments `production` (main only), `demo-unsafe` (reviewer) and `preview`; secrets set
- [ ] **NOT VERIFIED** (code and fake-backed unit tests only): `unlabeled` cleanup path; `synchronize` redeploy of an existing preview (`imageUrl`); cleanup-race branches; fork and Dependabot guards on real events; `gate` timeout; `rollback.yml`; `demo-unsafe-deploy.yml`; `perf.yml`; scheduled run of `demo-fixtures.yml`; Dependabot
- [x] `demo-fixtures.yml` (scheduled `--validate-fixtures`) written and actionlint-clean; the scheduled run on GitHub is **NOT VERIFIED**
- [ ] Review of other inherited env vars/secrets on previews before opening previews to more contributors
- [ ] SHA pins verified against the GitHub API by the architect: not done (devops/security responsibility)

## Phase 8 - Defect injection (demo-engineer)
- [x] Engine: list / show / inject / status / reset / verify / verify-all / validate-fixtures / image-guard; ops replace_text, replace_regex, create_file, replace_file; `--force`; `--allow-runtime-failures`
- [x] 13 scenarios (`clean`, `bad_lint`, `bad_format`, `bad_typecheck`, `bad_unit_test`, `bad_integration_test`, `bad_coverage`, `bad_security`, `bad_dependency`, `bad_container`, `slow_api`, `unhealthy_health`, `bad_deployment`)
- [x] tests/demo (117 demo-marked tests); baseline-clean guard passes
- [x] `demo verify-all`: **13/13 PASS, 0 skipped** (run by the architect, 2026-10-10)
- [ ] `python -m demo validate-fixtures` against live advisory databases: not re-run in this pass
- [x] Makefile `demo` / `demo-reset` forward `DEMO_ARGS` (present in the Makefile at last check; not exercised by the architect)

## Phase 9 - Documentation (documentation)
- [x] README.md (391 lines), docs/QUALITY_GATES.md, docs/DEMO_RUNBOOK.md, docs/SETUP_GITHUB_RENDER.md, docs/TROUBLESHOOTING.md, docs/SECURITY.md exist (presence checked by the architect)
- [ ] Content accuracy and "every documented command executed and verified": not reviewed by the architect; owned and reported by documentation
- [ ] The 35-45 minute DEMO_RUNBOOK timeline has not been rehearsed end to end by the architect (NOT VERIFIED)

## Phase 10 - Integration and release readiness
- [x] Clean baseline passes: `pytest -q --cov` (1431 pass, 100 % branch), lint, format, types, `make policy-check`, actionlint; `make security` passed earlier in this session and was not repeated in this pass
- [x] `make docker-test` green on the linux/amd64 image
- [x] `demo-verify-all` green; `demo-reset` leaves a clean tree (`no scenario active`, no markers)
- [ ] `make quality` as a single command: its components passed individually; the single-command run was
      performed inside `demo verify-all` (`clean` scenario, PASS in 435 s), not separately by the architect
- [ ] Trivy image gate on the final tarball per policy: earlier devops run only, not re-run here
- [x] actionlint clean; [ ] action SHA pins verified against the GitHub API by the architect: not done
- [x] First real release to GitHub/Render: done (see Phase 7). [ ] Rollback rehearsal (`rollback.yml`): **NOT VERIFIED**
- [ ] Open questions resolved by the project owner (public GHCR package, demo-unsafe environment reviewers, Render plan)
