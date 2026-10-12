# Troubleshooting

Entries marked **(seen)** were reproduced on the dev VM while writing the documentation. Entries marked
**(from the code/docs)** are derived from the workflow files, scripts or the vendors' documentation and were
not reproduced (no GitHub or Render account was touched). Presenter-time recovery is also covered in the
"what if it goes wrong" table of [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md#7-what-if-it-goes-wrong).

Golden rules: read the failing log first; never lower a threshold, add an ignore or bypass branch protection to
get past a red gate (fix the code); never commit a `DEMO-DEFECT:` marker to `main`.

## 1. Local development

| Symptom | Cause | Fix |
|---|---|---|
| `uv: command not found`, `trivy`/`k6` not found | `~/.local/bin` is not on `PATH` (the Makefile exports it for its own recipes, your shell does not) | `export PATH=$HOME/.local/bin:$PATH`. Missing tools: `make tools` (pinned, checksum-verified trivy, actionlint, k6). |
| `make sync` or `uv sync --frozen` fails: lockfile needs updating | `pyproject.toml` changed without `uv lock` | `uv lock`, commit `uv.lock`; `make lock-check` must pass. (seen: changing only `version = "0.1.0"` makes `uv lock --check` fail with `The lockfile at uv.lock needs to be updated`; `uv lock` prints `Updated stockflow v0.1.0 -> v0.1.1` and fixes it.) |
| `make test` takes minutes | The suite is large (1431 tests incl. concurrency and demo-engine tests) and shares the CPU with whatever else runs | (seen: about 40 seconds alone, several minutes under load). Use `make test-unit` or a single file (`uv run pytest -q tests/unit`, under a second for pricing/validation tests) while iterating. |
| `make test-coverage` fails: `Required test coverage of ... not reached` | New code without tests (or the `bad_coverage` scenario is active) | Add tests. **Do not** change `coverage.fail_under` (policy floor; `make policy-check` fails on a downgrade). (seen with `bad_coverage`: `app/services/legacy_import.py ... 0%`, `Total coverage: 78.70%`.) |
| `make lint` / `format-check` / `typecheck` fails after you edited | Ordinary findings | `uv run ruff check --fix .`, `make format` (applies `ruff format`; not itself a gate), fix mypy errors. |
| `make policy-check` fails | A hard-coded threshold in `Makefile`/workflows/`scripts/deploy`, an unpinned action, a forbidden trigger, an expired or malformed ignore, a write permission outside the allowed job | Read the message; move the value into `config/quality-policy.yaml` (security owns it) or repin/remove the offending line. Never edit around the check. |
| `make workflow-lint`: `actionlint: command not found` | Not installed | `make tools`. |
| Address already in use on `make run` / `docker-run` | Port taken | `PORT=8001 make run`; for Docker `make docker-run HOST_PORT=8001`. |
| `GIT_SHA` is forty zeros in the image | The repository has no commit yet (the Makefile uses that placeholder because the smoke test rejects the literal `unknown`) | Commit, or pass `GIT_SHA=<40 hex>`. |
| Local `/api/version` shows `0.0.0-dev` / `unknown` | Normal outside the image: version and sha are baked in as build args | Use the Docker image (`make docker-test` shows version `0.1.0` and the real sha). (seen) |

## 2. Security gates

| Symptom | Cause | Fix |
|---|---|---|
| `make security-audit` fails on a dependency you did not touch | A new advisory was published (databases are fetched at run time) | Upgrade the dependency (`uv lock --upgrade-package <name>`) and commit `uv.lock`. A time-boxed ignore needs id, reason and expiry in `config/quality-policy.yaml` (`pip_audit.ignore`) via the security owner. |
| `trivy fs` fails on `uv.lock` | Same, HIGH/CRITICAL advisory in a dependency | Same fix. (seen with `bad_dependency`: `Total: 5 (HIGH: 5, CRITICAL: 0)`, `CVE-2024-24762` and four more for `python-multipart 0.0.6`.) |
| `trivy image` fails on a clean checkout | The pinned base image digest now has a *fixable* HIGH/CRITICAL OS finding (unfixed ones do not block) | Refresh the base image digests in the Dockerfile (Dependabot proposes them), rebuild. Do not ignore. |
| Trivy cannot download its database / is slow | No network or registry rate limit | Retry on a working connection (the DB is downloaded from a registry); `make demo-fixtures` exits 3 for "could not verify", which is not a pass. |
| `trivy image` on the arm64 dev VM: `content digest ... not found` | Containerd image store with a foreign-platform (amd64) image | Use the tarball route the Makefile already uses: `make docker-save` then `make trivy-image-tar` (or `make docker-scan`). Documented in [SECURITY.md](SECURITY.md) section 8. |
| Bandit fails on `shell=True`, B602 | A real finding (seen with `bad_security`: `Severity: High Confidence: High`, `app/services/metrics.py:33:11`) | Use an argument list without a shell and validate the input. A justified inline `# nosec B### - reason` is reviewed in the diff; global skips are forbidden. |
| `make security-trivy-config` flags the Dockerfile | A misconfiguration such as a missing `USER` | Fix the Dockerfile; HIGH/CRITICAL blocks. |
| A fresh `.trivyignore` or policy entry fails `policy-check` | Missing `# justification:`, expiry in the past, or expiry further away than the policy window | Provide id, reason and a near expiry. |

## 3. Docker

| Symptom | Cause | Fix |
|---|---|---|
| `permission denied` talking to the Docker daemon | The user is not in the `docker` group (dev VM) | The Makefile auto-prefixes `sudo` for `docker-*` targets; to force: `make docker-test DOCKER="sudo docker"` or `DOCKER=docker`. |
| `make docker-test` is slow or the container takes ~20 s to become healthy | amd64 image emulated on an arm64 host | Expected (seen: healthy within the 180 s budget, stopped in 3 s). Native amd64 CI runners do not emulate. `START_TIMEOUT` raises the budget. |
| `make docker-test` fails at `FAIL  no DEMO-DEFECT marker in /app` | A demo scenario is active (or a marker was committed) | `make demo-reset`, rebuild. (seen: the image guard correctly refuses an image built from an injected tree.) |
| `docker-test` fails with `health status: ... exited` | The container crashed: run `sudo docker logs stockflow-test` (the script prints the last 40 log lines) | Typical causes: `PORT` not an integer, a code error, a fault scenario with both env keys set. |
| Compose build is for the wrong platform | `compose.yaml` builds for the host platform by design (tag `stockflow-compose:local`) | For the production platform use `make docker-build` (amd64). |
| Image contains something it should not | The allow-list `.dockerignore` was loosened | Restore `*` then `!app`, `!pyproject.toml`, `!uv.lock`; `make docker-guard` explains which check failed. |

## 4. Defect injection (`python -m demo`)

| Symptom | Cause | Fix |
|---|---|---|
| `make demo` exit 2: `... already contain DEMO-DEFECT: markers but there is no .demo-state/state.json, so the engine cannot restore them` | The target file already carries a committed defect (you are on a `demo/*` branch) or the state was deleted (seen) | Restore the files from version control (`git switch -f main`), then inject again. |
| `make demo-reset` exit 2 listing files | You edited a file the active scenario owns (hash neither injected nor original) | Save your work, then `make demo-reset DEMO_ARGS=--force`; the discarded copy is kept in `.demo-state/discarded/<timestamp>/`. |
| `make demo SCENARIO=slow_api` is refused | Runtime scenarios are off by default | `make demo SCENARIO=slow_api DEMO_ARGS=--allow-runtime-failures` (seen). |
| An injected runtime fault does nothing | By design inert unless the process has **both** `STOCKFLOW_DEMO_FAULT=<id>` and `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true` (seen: 0.02 s with no keys, 0.79 s with them for `slow_api`) | Start the app with both variables. On Render set both on the service. |
| After `make demo-reset` a `.demo-state/` directory remains | The engine leaves an empty `backup/` and a `lock` file; it is git-ignored | Harmless. The reset is complete when `make demo-status` prints `baseline: no scenario active` and `git status` is empty. (seen) |
| `make demo-status` warns `app/... contains DEMO-DEFECT markers but no state exists` | You switched to a branch with a committed defect | `git switch -f main`. |
| `make demo-fixtures` exit 1 or 3 | 1: an advisory or base image no longer triggers the scanner (stale fixture). 3: could not verify (offline) | Do not demo that scenario until fixed (demo owner updates the fixture). |
| `make demo-verify SCENARIO=...` reports SKIPPED | A requirement (docker, trivy, k6, network) is missing | Install it; SKIPPED is never reported as passed. |
| `tests/demo/test_baseline_clean.py` fails on `main` or a feature branch | A `DEMO-DEFECT:` marker is in the tree | Remove it (`make demo-reset` or revert). The test deliberately skips only on `demo/<scenario>` branches or with an active local scenario. |

## 5. GitHub Actions

All **(from the code/docs)**.

| Symptom | Cause | Fix |
|---|---|---|
| PR shows no checks | Actions disabled for the repo/fork, workflow file not on the base branch, first-time-contributor approval pending | Settings > Actions; approve the run. |
| `quality-gate` red but the failing job is not obvious | A needed job was skipped or cancelled (counts as failure) | Open the run summary: the `## Quality gate` table lists each job as PASS/FAIL with its result. |
| Branch protection cannot find `quality-gate` | The check has never run in this repository | Run CI once, then add the check. Require only `quality-gate`. |
| Merge button disabled on your own PR | Required approvals set above 0 for a solo repo | Set required approvals to 0 (SETUP 1.3). |
| `release` is red in the `deploy` job: `Deployment is mandatory but not configured` / `DEPLOYMENT FAILED: not configured (deployment is mandatory)` | Hook secret `RENDER_DEPLOY_HOOK_URL` (`production` environment) or variable `STOCKFLOW_BASE_URL` missing. Expected on the very first push: the image was still built, scanned and published to GHCR before this failure, and `release-record.json` was uploaded | Follow the bootstrap in [SETUP_GITHUB_RENDER.md](SETUP_GITHUB_RENDER.md#4-bootstrap--first-push-and-first-release), then either *Re-run failed jobs* on the original run (redeploys the already-published digest, no rebuild) or run `release` via workflow_dispatch on `main`. Never silence it: `require_deploy` defaults to true and `false` (publish without deploying) only warns and records `deployed=false`. |
| `release` red in `deploy` at "Assert release record == digest sent to Render == what is serving" (`MISMATCH: ...`) | `verify-deployment` found that the release record, the digest sent in `imgURL` and the live `/api/version` (digest/commit/version/name) disagree, or Render did not report the deploy `live` | Read the `MISMATCH:` lines in the log and the job summary (`Deployment verification FAILED`); check Render Events for which image is actually serving; do not re-run until the cause is understood. |
| `publish` fails pushing to GHCR (denied / 403) | The package exists and the repository has no write access, or it was created by someone else first | Package settings > Manage Actions access: add the repository with Write. |
| `publish`: "the sha tag and the version tag resolved to different digests" | A concurrent push moved a tag | Re-run; deploys are serialised by `deploy-production`. |
| `deploy` waits | `production` environment needs a reviewer | Review deployments > Approve. |
| `rollback` / `release` queued for a long time | Concurrency group `deploy-production` is held (`cancel-in-progress: false`) by another run (often `demo-unsafe-deploy`) | Wait or cancel the holding run. |
| `rollback` fails: "A rollback that cannot run is a failure" | Hook secret / base URL missing | Configure `RENDER_DEPLOY_HOOK_URL` (environment `production`) and `STOCKFLOW_BASE_URL`. |
| `rollback`/`demo-unsafe-deploy` `verify`/`guard`: image not found | Wrong digest, wrong owner case, or package private | Digest format is `sha256:<64 hex>`; copy it from `release-record` or the package page; owner is lowercase. |
| `demo-unsafe-deploy`: "type exactly DEPLOY-BROKEN-IMAGE" | Confirmation mismatch | Retype it exactly. |
| Workflow does not start from a branch | `rollback`, `demo-unsafe-deploy` and `release` guards require `refs/heads/main` | Run the workflow from `main`. |
| `policy-check` fails in CI only | A workflow gained a write permission, an unpinned action, or `pull_request_target` | Revert it or follow the policy ([SECURITY.md](SECURITY.md) sections 3 and 4). |
| Actions pin verification | Pins must be full 40-hex SHAs with the tag in a comment | See [SECURITY.md](SECURITY.md) section 3 for the verification procedure. |

## 6. Render and the running service

Render facts below come from Render's documentation (read 2026-10-10); behaviour was not observed live.

| Symptom | Cause | Fix |
|---|---|---|
| Hook returns 404 (or 400) | `imgURL` is invalid: everything except the tag/digest must equal the service's image URL | The service must be `ghcr.io/sixseven-pranav-jothivel/stockflow:<tag>`; recreate it if it was created with another host, owner case or repository. |
| Hook returns 401 | `key` missing/invalid: the hook was regenerated | Copy the new hook from Render and update the secret. |
| Hook returns 409 | Service suspended (for example free instance hours used up) or workspace cannot deploy | Resume/upgrade in Render. |
| Hook returns 202 | Another deploy is running; Render queues this one (only with the *Wait* overlapping-deploy policy) | Wait; the workflow then verifies by sha. |
| Render cannot pull the image | The GHCR package is private | Make it public (SETUP 1.6), or add a registry credential with `read:packages` on Render. |
| Deploy never becomes live; old version keeps serving | New instance fails `/health` (or never starts); Render cancels after 15 minutes and keeps routing to the old instance | Read the Render deploy logs. Fix forward, or run `rollback.yml` if the bad one is live. Render does not roll back by itself. |
| The bad deployment is live | The image passed `/health` but is wrong | `rollback.yml` with the previous good digest. |
| First request after idle takes about a minute | Free instances spin down after 15 minutes without traffic | Warm up beforehand (`make smoke BASE_URL=...`). |
| Data disappears | Expected: in-memory repository; every restart/deploy/spin-down resets it | The dashboard banner says so. Two instances would also show different data: keep exactly one. |
| Free hours exhausted | 750 free instance hours per workspace per month shared by all free services | Wait for the next month, suspend unused services, or upgrade. |
| Health check fails although the container works | Health check path is wrong or the app binds the wrong port | Path `/health`; the app binds `0.0.0.0:$PORT` and Render supplies `PORT` (do not hard-code it). Checks must answer within 5 seconds. |
| Service does not offer the Free type for an image | A free image-backed service worked on 2026-10-12, but plan availability can change | Choose another instance type; the pipeline does not depend on the plan. |

## 7. Smoke and performance checks

| Symptom | Cause | Fix |
|---|---|---|
| `[FAIL] version: ... git_sha mismatch: expected ..., deployed ... (old version still serving, or the wrong build was deployed)` | Deploy not finished, or wrong digest | Wait/retry (the test retries until `--timeout`); check Render Events and `/api/version`. |
| `[FAIL] health: GET /health: expected HTTP 200, got 503` (seen) | Unhealthy instance (the `unhealthy_health` scenario armed, or a real failure) | Remove the fault variables / roll back; `SMOKE TEST FAILED: health`, exit status 1. |
| Smoke test takes a minute to fail locally | Default retry budget is 60 s per check | `--timeout 5` for a quick local run: `uv run python scripts/smoke_test.py --base-url ... --timeout 5`. |
| Smoke test usage error (exit 2) | `--base-url` without `http://`/`https://`, or a negative timeout | Fix the argument. |
| `make perf` ends `make: *** [Makefile:...: perf] Error 99` | k6 exit code 99: a threshold was crossed (seen with `slow_api`: `p(95)=765.73ms`) | Expected for the defect; otherwise investigate latency. Thresholds come from the policy (`load_test.*`). |
| `make perf` has errors against Render | Free instance (small CPU) or cold start | Run against a local instance; `perf.yml` against Render is opt-in and writes data. |
| k6: `setup failed: POST /api/products -> ...` | Target unreachable or the API changed | Check `BASE_URL`; the test creates its own product and deactivates it afterwards. |

## 8. PR previews (pr-preview.yml)

The label-then-close path was observed on GitHub and Render on 2026-10-12 (PR #3, runs 38209130008 and 38209469656). The other rows are **from the code/docs, NOT VERIFIED** live (the `unlabeled` and `synchronize` paths, the cleanup race branches, the fork/Dependabot guards on real events and the gate timeout). Render API facts come from the comments in `scripts/deploy/preview_lib.py` (Render's OpenAPI document and service-previews page).

| Symptom | Cause | Fix |
|---|---|---|
| Labelled PR, `preview` job red: `NOT DEPLOYED: preview not configured` | Secrets `RENDER_API_KEY` and/or `RENDER_SERVICE_ID` missing from the `preview` environment | Create the environment and set both secrets ([SETUP 8](SETUP_GITHUB_RENDER.md#8-optional-per-pr-preview-environments)), then re-run failed jobs or push to the PR. |
| Adding the label does nothing | PR is from a fork or from Dependabot (never eligible); the label is not exactly `preview`; PR does not target `main`; Actions disabled | Use a branch in this repository; check the label name; the sticky comment is absent for skipped PRs by design. |
| Sticky comment says `No preview` / `quality-gate FAILED for this commit` | The gate for the head commit is red (for example a `demo/<scenario>` defect PR) | Fix the PR; the next push re-runs. A preview never deploys from a red gate. |
| `gate` job times out | `quality-gate` never appeared or took more than the 25-minute wait | Check that `ci` ran for the head commit; push again. |
| Comment says `No preview: image build, scan or publish failed` | The image build, the container smoke test or image guards, the Trivy image gate or the GHCR push failed | Open the `build-scan`/`publish` job logs; same causes as for the release image (sections 2, 3 and 5). |
| `Preview deployment FAILED`, image published | Render API error: invalid key or service id, base service is not image-backed, plan not allowed, deploy not live in time (the `@sha256` digest form of `imagePath` and a free-plan preview of a free base service were accepted on 2026-10-12) | Read the `preview` job log (the key is masked). Set `PREVIEW_PLAN` if the plan is the issue. Do not work around a digest problem with a mutable tag; report it. |
| Preview smoke test fails: `git_sha mismatch` | Old image still serving, or the deploy did not take the new digest | Wait for the retry window; check the preview's Events in Render. |
| Preview dashboard or `/api/version` says environment `production` | Previews inherit the base service's env vars (confirmed 2026-10-12) | Expected cosmetic limitation; the version string `<version>-pr<n>` and the PR head sha identify the preview. |
| Extra `pr-preview` run for every closed PR | `closed` always starts the cleanup path; it exits quickly when there was no preview | Expected. |
| `gh secret set ... --env production` worked but the preview still says not configured | GitHub secrets cannot be read back or copied between environments; `preview` needs its own `RENDER_API_KEY` and `RENDER_SERVICE_ID` | `gh secret set RENDER_API_KEY --env preview` and `gh secret set RENDER_SERVICE_ID --env preview`. |
| Preview data differs from production / resets | Each preview is its own service with in-memory data | Expected. |
| Comment says `Preview cleanup FAILED` | Cleanup job error (API key revoked, Render outage) | Delete the Render service `stockflow-pr-<n>` by hand in the dashboard; Render never deletes image previews itself. |
| Previews pile up in Render | PRs closed while Actions was disabled, or cleanup failed | List services named `stockflow-pr-*` in Render and delete the ones whose PR is closed. |
| Two runs fight for one PR | By design a newer update cancels the older run; `closed`/`unlabeled` runs never cancel and queue behind it | Wait. |
| Preview runs for a PR but should not (trust) | The workflow file comes from the PR merge commit with `pull_request`, so anyone with write access can alter it | Restrict who can push branches; put required reviewers on the `preview` environment (SETUP 8). |
