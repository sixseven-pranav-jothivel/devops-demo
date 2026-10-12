# Quality gates

**Source of truth: [`config/quality-policy.yaml`](../config/quality-policy.yaml).** This document explains how
the gates are wired; it deliberately does not restate thresholds. To see a value, ask the policy:

```
$ uv run python scripts/quality_policy.py get coverage.fail_under
85
$ uv run python scripts/quality_policy.py get load_test.p95_ms
500
$ uv run python scripts/quality_policy.py args bandit
-r app -c config/bandit.yaml --severity-level medium --confidence-level medium
$ uv run python scripts/quality_policy.py args k6
-e P95_MS=500 -e ERROR_RATE_MAX=0.01 --vus 10 --duration 30s
```

(Outputs above are from the repository at the time of writing; if the policy changes, the policy wins.)
Nothing is typed twice: the Makefile and the workflows read the policy through `scripts/quality_policy.py`,
and `make policy-check` fails on drift (a hard-coded threshold in `Makefile`, `.github/workflows` or
`scripts/deploy`), on expired ignores, and on any attempt to weaken a floor. The one unavoidable mirror,
`[tool.coverage.report] fail_under` in `pyproject.toml`, is verified equal to the policy by the same check.

Rule of the project: **never weaken a gate to make it pass; fix the code.** Ignores need an id, a
justification and an expiry (`pip_audit.ignore`, `.trivyignore`; both are empty on the baseline).

## 1. Matrix

| # | Gate | Make target (local) | CI job in `ci.yml` | Policy key(s) | What blocks |
|---|---|---|---|---|---|
| 1 | Policy, mirrors, workflow permissions, drift scan | `make policy-check` | `config-validation` | whole file (`check`) | PR |
| 2 | Lockfile in sync with `pyproject.toml` | `make lock-check` | `config-validation` | none (uv) | PR |
| 3 | Workflow lint | `make workflow-lint` (actionlint) | `config-validation` | none | PR |
| 4 | Lint | `make lint` (`ruff check .`) | `lint-format` | `ruff.check`, `ruff.paths` | PR |
| 5 | Format | `make format-check` (`ruff format --check .`) | `lint-format` | `ruff.format_check` | PR |
| 6 | Types (strict) | `make typecheck` (`mypy`) | `typecheck` | `mypy.strict`, `mypy.paths` | PR |
| 7 | Unit tests | `make test-unit` | `unit-tests` | none | PR |
| 8 | Integration tests (API, services, repositories, invariants, concurrency) | `make test-integration` | `integration-tests` | none | PR |
| 9 | Coverage (branch) | `make test-coverage` | `coverage` | `coverage.fail_under`, `coverage.branch`, `coverage.source` | PR |
| 10 | SAST | `make security-bandit` | `bandit` | `bandit.*` (`severity`, `confidence`, `fail_on_findings`, `config`) | PR |
| 11 | Dependency audit | `make security-audit` | `dependency-audit` | `pip_audit.*` (`scope`, `fail_on_any`, `ignore`) | PR |
| 12 | Filesystem scan (vulnerabilities and secrets) | `make security-trivy-fs` | `trivy-fs-scan` | `trivy.fs.*`, `trivy.scanners_fs`, `trivy.skip_dirs` | PR |
| 13 | Config scan (Dockerfile, compose, IaC) | `make security-trivy-config` | `trivy-fs-scan` | `trivy.config.*` | PR |
| 14 | Image build, container smoke test, image guard, graceful stop | `make docker-test` | `docker-build` | `trivy.image.platform` (target platform) | PR |
| 15 | Image scan (amd64, vulnerabilities and secrets) | `make docker-scan` (= `docker-save` + `trivy-image-tar`) | `trivy-image-scan` | `trivy.image.*` | PR and release |
| 16 | Final aggregate | n/a | **`quality-gate`** | n/a | the only required status check |
| 17 | Post-deploy verification (smoke test with `--expect-sha`, then `release_tools.py verify-deployment`) | `make smoke BASE_URL=... EXPECT_SHA=...` | `release.yml` `deploy` job (mandatory on push to `main`; also fails when the hook secret or `STOCKFLOW_BASE_URL` is missing) | `smoke.timeout_seconds`, `smoke.poll_interval_seconds` | marks the release run red; rollback is manual |
| 18 | Load test (opt-in) | `make perf BASE_URL=...` | `perf.yml` (manual; run for real 2026-10-12, run 38214612234, thresholds passed) | `load_test.p95_ms`, `load_test.error_rate_max`, `load_test.vus`, `load_test.duration` | informational; the demo's slow_api scenario |
| 19 | PR preview image (opt-in, label `preview`; main paths run on GitHub/Render 2026-10-12; red-gate, fork-guard and timeout paths NOT VERIFIED) | `make docker-test`, `make docker-save`, `make trivy-image-tar` (the same targets, run by `pr-preview.yml` `build-scan`) | `pr-preview.yml` jobs `gate`, `build-scan`, `publish`, `preview` | `trivy.image.*`, `smoke.*` | not a merge gate: it waits for a green `quality-gate` and never changes it |

Aggregates: `make security` = `policy-check` + 10 + 11 + 12 + 13. `make quality` = lint + format-check + typecheck
+ test-coverage + security. These are the local equivalents of the PR gates, except that they do not build or
scan the Docker image (rows 14 and 15; use `make docker-test` and `make docker-scan`).

### PR previews and the gate

`pr-preview.yml` is not part of `quality-gate` and no branch-protection check refers to it. It only **consumes**
`quality-gate`: its `gate` job polls the check run of that name for the PR's exact head commit and the preview is
built and deployed only if it is green (a red or cancelled gate yields a sticky comment "No preview"). The image
it builds goes through the same Make targets as the release image (`docker-test` with the image guards,
`docker-save`, `trivy-image-tar`) so the same `trivy.image.*` policy applies. Since it is not required for merging,
a failing preview never blocks a PR. Observed on GitHub on 2026-10-12 (PR #3: the `gate` job waited for a green `quality-gate` and then the preview deployed); the red-gate path was not exercised there. See [SETUP_GITHUB_RENDER.md](SETUP_GITHUB_RENDER.md#8-optional-per-pr-preview-environments).

### How `quality-gate` works

`quality-gate` runs with `if: always()` and `needs:` every other job. It calls
`scripts/deploy/release_tools.py gate`, which passes only if each needed job finished exactly `success`.
Failed, cancelled, skipped and missing jobs all fail the gate (so a job that was skipped because its
dependency failed cannot sneak through). Branch protection therefore needs exactly one required status
check: `quality-gate` (see [SETUP_GITHUB_RENDER.md](SETUP_GITHUB_RENDER.md)). The job summary lists every job
with PASS or FAIL.

### Non-gating but always produced

* `reports/trivy-image-full.json`: the complete image report including findings that have no fix yet
  (`args trivy-image-report`, non-blocking, uploaded as an artifact so nothing is hidden).
* Reports from each scanner are uploaded as workflow artifacts (30 days); release and rollback/deployment
  records for 90 days.

## 2. Gate semantics worth knowing

* **Image scan and unfixed findings.** The image scan blocks on HIGH/CRITICAL findings that **have a fix**
  (`trivy.image.ignore_unfixed: true`); the filesystem scan and pip-audit have no such relaxation. The reason
  and the evidence are in [SECURITY.md](SECURITY.md) section 7.2; it is the only relaxation in the policy.
* **pip-audit scope** is runtime plus dev dependencies from `uv export` of the lock (dev tooling is attack
  surface in CI too). Any known vulnerability fails regardless of severity.
* **Bandit** reports and fails at the policy severity and confidence and above. Suppressions are inline
  `# nosec B### - reason` comments (reviewed in the diff); global skips are forbidden.
* **Coverage** is branch coverage over `app/`. Lowering `fail_under` fails `policy-check` because of the floor
  check and the pyproject mirror.
* **Demo branches.** The baseline guard test (`tests/demo/test_baseline_clean.py`) fails if a `DEMO-DEFECT:`
  marker is present, except on branches named `demo/<scenario>` (CI passes `GITHUB_HEAD_REF` / `GITHUB_REF_NAME`)
  or when a scenario is injected locally. That is why demo branches fail only at their declared gate, and why
  the same marker on `main` fails the build.
* **The image guard** (part of `docker-build`) also fails any image whose `/app` contains a `DEMO-DEFECT`
  marker. Consequence: a demo PR that patches files under `app/` is red in `docker-build` as well as in its
  declared gate, and `trivy-image-scan` is then skipped (it `needs` `docker-build`). `quality-gate` is red either
  way. This was checked locally for `unhealthy_health` (`make docker-guard` printed
  `FAIL  no DEMO-DEFECT marker in /app`); for the other `app/` scenarios it follows from the same grep.

## 3. Which gate catches which defect scenario (verified locally)

Each row was reproduced with the real tool in a scratch copy of the repository (the live tree was never
modified). "CI jobs" maps the failing Make target onto the `ci.yml` job that runs it; it was not observed on
GitHub. Make exits with status 2 when the underlying tool exits non-zero (k6 exit code 99 shows up as
`make: *** [Makefile:...: perf] Error 99`).

| Scenario | Failing command (verified) | Signature in the output | Failing CI job(s) |
|---|---|---|---|
| `bad_lint` | `make lint` | `F401 [*] \`os\` imported but unused` / `app/services/orders.py:5:8`, `Found 1 error.` | `lint-format` |
| `bad_format` | `make format-check` | `File would be reformatted` / `app/services/pricing.py`, `1 file would be reformatted, 72 files already formatted` | `lint-format` |
| `bad_typecheck` | `make typecheck` | `[return-value]` in `app/services/products.py:19` and `app/api/products.py:51`, `Found 2 errors in 2 files` | `typecheck` |
| `bad_unit_test` | `uv run pytest -q tests/unit`; `make test-unit`; `make test-integration` | 17 failed in `tests/unit/test_pricing.py` (unit); 24 failed integration (order totals) | `unit-tests`, `integration-tests`, `coverage` |
| `bad_integration_test` | `make test-integration` | 26 failed (stock not decremented: invariants, services); `make test-unit` still passes (360 passed) | `integration-tests`, `coverage` |
| `bad_coverage` | `make test-coverage` | `app/services/legacy_import.py 190 190 68 0 0%`, `FAIL Required test coverage of 85% not reached. Total coverage: 78.70%` | `coverage` |
| `bad_security` | `make security-bandit` | `B602:subprocess_popen_with_shell_equals_true`, `Severity: High Confidence: High`, `app/services/metrics.py:33:11` | `bandit` |
| `bad_dependency` | `make security-audit`; `make security-trivy-fs`; `make lock-check` still passes | pip-audit lists `python-multipart 0.0.6` (fix versions such as 0.0.7); trivy fs `Total: 5 (HIGH: 5, CRITICAL: 0)` incl. `CVE-2024-24762` | `dependency-audit`, `trivy-fs-scan` (the image scan would also see the package, not run here) |
| `bad_container` | `make docker-build`, `make docker-save`, `make trivy-image-tar` | `reports/image.tar (debian 12.2)  debian  89` vulnerabilities, non-zero exit | `trivy-image-scan` |
| `slow_api` | `make perf BASE_URL=...` with both env keys set | `✗ 'p(95)<500' p(95)=765.73ms`, `thresholds on metrics 'http_req_duration' have been crossed` | none in PR CI (runtime scenario); `perf.yml` when run manually |
| `unhealthy_health` | `make smoke BASE_URL=...` with both env keys set | `[FAIL] health: GET /health: expected HTTP 200, got 503: '{"status":"unhealthy"}'`, `SMOKE TEST FAILED: health` | none in PR CI; the release `deploy` job's smoke step |
| `bad_deployment` | `STOCKFLOW_DEMO_FAULT=bad_deployment STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true uv run python -m app` | `RuntimeError: simulated startup failure (STOCKFLOW_DEMO_FAULT=bad_deployment)`, exit 1 | none in PR CI; Render health check / release smoke step |

Numbers in the third column (counts, percentages, millisecond values) are single observations from one run on
the dev VM and will vary slightly; the failing command and the signature strings are what is stable.

## 4. Re-verify the gates yourself

```bash
export PATH=$HOME/.local/bin:$PATH
make quality                         # green on the clean baseline (verified, exit 0)
make demo-fixtures                   # the injected CVE / old base image still trigger the scanners (needs network)
make demo-verify SCENARIO=bad_lint   # inject -> gate fails as declared -> reset -> gate passes
make demo-verify-all                 # every scenario, about ten minutes, writes reports/demo-verify.json
```

`make demo-fixtures` output on the baseline (verified):

```
[demo] validating fixtures against the real tools ...
[demo]   patch    bad_lint               OK: patch applies cleanly to the current baseline
...
[demo]   pip-audit        bad_dependency OK: pip-audit reports ['CVE-2024-24762'] (26 advisory ids total)
[demo]   trivy-fs         bad_dependency OK: trivy fs reports ['CVE-2024-24762'] at HIGH/CRITICAL (5 findings)
[demo]   lock-consistency bad_dependency OK: patched pyproject.toml + uv.lock pass `uv lock --check`
[demo]   trivy-image-ref  bad_container OK: python:3.12.0-slim-bookworm has 92 fixable HIGH/CRITICAL findings
[demo] all fixtures valid.
```

Exit code 3 from this command means "could not verify" (for example offline) and is not a pass.

## 5. Adding or changing a gate

1. Put the threshold or flag in `config/quality-policy.yaml` (security owns it) and add a `get`/`args` consumer.
2. Add a Make target that reads it (devops), then a `ci.yml` job, then add the job to `quality-gate`'s `needs`
   **and** its `REQUIRED_JOBS` list. A job that is not in `needs` is not part of the gate.
3. Run `make policy-check` and `make workflow-lint`.
4. Document it in the matrix above.
