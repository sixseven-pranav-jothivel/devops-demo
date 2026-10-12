# Demo runbook: the StockFlow delivery lifecycle in 35 to 45 minutes

Presenter script for 14 stages: baseline, PR creation, lint error, failed unit test, security vulnerability
detection, fixing, successful merge, Docker build, GHCR publication, Render deployment, post-deploy smoke, an
optional performance regression, a controlled failed deployment, and a manual rollback by digest.

**What is verified and what is not.** Every local command and every output block below was run on the dev VM
against the real tools, in a scratch copy of the repository (the live tree was never modified); outputs are
trimmed, never invented. Everything that happens on GitHub (Actions UI, checks, environments, approvals,
GHCR page) and on Render (dashboard, deploys, health checks) is described from the workflow files and Render's
documentation, **not observed**, with exceptions observed for real on 2026-10-12 (the PR-preview paths and a production release run with Render API polling; see the optional stage P). `rollback.yml`, `demo-unsafe-deploy.yml` and `perf.yml` are not claimed as verified. Rehearse those parts once end to end before presenting
([SETUP_GITHUB_RENDER.md](SETUP_GITHUB_RENDER.md) first). Where this runbook estimates a duration it says so.

Contents: [1 Principles](#1-principles-and-honest-caveats) | [2 Preparation](#2-pre-demo-preparation) |
[3 Pre-flight checklist](#3-pre-flight-checklist) | [4 Timeline](#4-timeline-35-to-45-minutes) |
[5 Stages 1 to 14](#5-the-14-stages) | [6 Scenario cards](#6-remaining-scenario-cards) |
[7 What if it goes wrong](#7-what-if-it-goes-wrong) | [8 Clean-up](#8-after-the-demo)

## 1. Principles and honest caveats

* **Defects are real.** `make demo SCENARIO=<id>` edits real source files; the real tool then fails. Nothing is
  mocked. The clean baseline passes every gate (`make quality` exit 0, verified).
* **Demo branches are named `demo/<scenario>`** (for example `demo/bad_lint`). CI passes the branch name to the
  baseline guard test, which otherwise fails on any `DEMO-DEFECT:` marker. On a `demo/` branch the defect fails
  only at its declared gate (plus the image guard, see below). A defect on any other branch, or on `main`, also
  fails the guard test.
* **Never merge a commit that still contains `DEMO-DEFECT:`.** Fix it first (stage 6). The image guard in
  `docker-build` refuses an image containing a marker, so a defective app change also turns `docker-build` red,
  and `trivy-image-scan` is then skipped. `quality-gate` is red regardless.
* **Render does not roll back by itself.** A new instance receives no traffic until `/health` passes and the old
  instance keeps serving; if the new one is not healthy within 15 minutes, Render cancels the deploy. A bad image
  that still answers `/health` goes live. Recovery is the manual `rollback.yml` (stage 14). Say this plainly.
* **Demo data is not persistent.** Every deploy, restart or free-tier spin-down resets products and orders.
* **CI duration is unknown.** The full `ci.yml` (11 jobs) was never run on GitHub from here. Budget 4 to 8
  minutes per run as an *estimate*, and use pre-staged runs wherever the timeline needs a result quickly.
* **Some defects are invisible to some gates.** `bad_unit_test` (order total ignores the discount) is caught by
  the tests but passes `/health` and also the post-deploy smoke test and k6, which place single-unit orders under
  the discount tier. Do not claim the smoke test catches logic bugs.

## 2. Pre-demo preparation

### 2.1 One-time (the day before)

1. Complete [SETUP_GITHUB_RENDER.md](SETUP_GITHUB_RENDER.md): branch protection requiring only `quality-gate`;
   environments `production` and `demo-unsafe` with required reviewers; secrets and variables; package public;
   Render service created, health check `/health`, one instance. Prefer a **second Render service
   (`stockflow-demo`)** wired to the `demo-unsafe` environment so the broken-image demo cannot touch production.
2. Produce **release A**: run `release` on `main` (Actions > release > Run workflow; `require_deploy` defaults to true, leave it) so
   production serves a known-good image. (A push to `main` also requires a verified deployment and is red until
   the Render service and secrets exist; see the bootstrap in SETUP_GITHUB_RENDER.md.) Write down A's digest (`sha256:...`) from the run summary or the
   `release-record` artifact and its commit sha. A is the rollback target in stage 14.
3. **Validate the fixtures** against the live advisory databases (needs network):

   ```
   $ make demo-fixtures
   [demo] validating fixtures against the real tools ...
   [demo]   patch    bad_lint               OK: patch applies cleanly to the current baseline
   ...
   [demo]   pip-audit        bad_dependency OK: pip-audit reports ['CVE-2024-24762'] (26 advisory ids total)
   [demo]   trivy-fs         bad_dependency OK: trivy fs reports ['CVE-2024-24762'] at HIGH/CRITICAL (5 findings)
   [demo]   lock-consistency bad_dependency OK: patched pyproject.toml + uv.lock pass `uv lock --check`
   [demo]   trivy-image-ref  bad_container OK: python:3.12.0-slim-bookworm has 92 fixable HIGH/CRITICAL findings
   [demo] all fixtures valid.
   ```

   Exit code 1 names a stale fixture (the advisory was withdrawn or re-scored: do not demo that scenario);
   exit code 3 means "could not verify" (offline), which is not a pass.
4. Optionally prove any scenario end to end on your own machine (throw-away copy, about a minute each for the
   light ones): `make demo-verify SCENARIO=bad_lint`. `make demo-verify-all` takes about ten minutes.

### 2.2 Pre-stage the demo branches (the morning of, or earlier)

Create the branches locally with the engine, commit, push, open the PRs, and clean up. **Only `demo/bad_lint`
carries a legitimate change** (a version bump) so that its fixed PR is a real, mergeable change that visibly
changes what Render serves (version 0.1.1 and a new commit sha). Start from a clean `main`
(`git status` empty, `make demo-status` says baseline).

```bash
export PATH=$HOME/.local/bin:$PATH
git switch main && git pull

# --- demo/bad_lint: version bump + lint defect (used live in stages 2, 3, 6, 7)
git switch -c demo/bad_lint main
sed -i 's/^version = "0.1.0"/version = "0.1.1"/' pyproject.toml && uv lock   # "Updated stockflow v0.1.0 -> v0.1.1"
git commit -am "chore: bump version to 0.1.1"
make demo SCENARIO=bad_lint
git commit -am "demo: bad_lint (unused import)"
make demo-reset            # clears the local engine state; working tree is back at baseline
git switch -f main         # discards that baseline-equal edit
#   (do NOT push yet if you want to push live in stage 2)

# --- pre-staged red PRs, one defect each, branched from main (stages 4 and 5)
for s in bad_unit_test bad_security; do
  git switch -c demo/$s main
  make demo SCENARIO=$s
  git commit -am "demo: $s"
  git push -u origin demo/$s
  make demo-reset
  git switch -f main
done
```

Then open a PR from each pushed branch into `main` in the GitHub UI (or `gh pr create --base main --head
demo/bad_unit_test --title "demo: bad_unit_test" --body "demo"`; `gh` was not used here). CI starts. After the
runs finish you have red, ready-to-show PRs. Verified locally: the commit, `demo-reset` and `git switch -f`
sequence leaves `git status` empty and `make demo-status` at `baseline: no scenario active`.

Optionally add `demo/bad_format`, `demo/bad_typecheck`, `demo/bad_coverage`, `demo/bad_dependency` in the same
way as spare material for questions.

### 2.3 Pre-stage the failed-deployment demo (stage 13)

Do this once, well ahead (the red workflow run takes minutes; see stage 13).

1. On the **demo** Render service, set the two environment variables that arm the fault (they are inert for any
   image that does not contain the fault code, so the service keeps running its good image):
   `STOCKFLOW_DEMO_FAULT=unhealthy_health` and `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true`.
   Saving environment changes redeploys a Render service (not confirmed from Render's pages; check the
   dashboard); wait until it is live again.
2. Build and push the broken image yourself (the pipeline never builds one). Commit the defect first so the image
   carries a commit sha different from `main`'s:

   ```bash
   OWNER=sixseven-pranav-jothivel
   git switch -c demo/unhealthy_health main
   make demo SCENARIO=unhealthy_health DEMO_ARGS=--allow-runtime-failures
   git commit -am "demo: unhealthy_health"
   read -rs GHCR_TOKEN        # classic PAT with write:packages; never paste it into the repo
   echo "$GHCR_TOKEN" | docker login ghcr.io -u <your-github-username> --password-stdin
   docker buildx build --platform linux/amd64 --provenance=false --sbom=false --push \
     -t ghcr.io/$OWNER/stockflow:demo-unhealthy-health \
     --build-arg APP_VERSION=0.1.0-demo --build-arg GIT_SHA=$(git rev-parse HEAD) \
     --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
     --label org.opencontainers.image.source=https://github.com/$OWNER/devops-demo .
   #   ... #16 pushing manifest for ghcr.io/<owner>/stockflow:demo-unhealthy-health@sha256:<64 hex>
   make demo-reset && git switch -f main
   ```

   (Prefix `docker` with `sudo` on the dev VM.) The digest is the `sha256:...` in the line
   `pushing manifest for ...@sha256:<digest>`. `--provenance=false --sbom=false` matters: without it a
   containerd-backed Docker stores an index with an attestation manifest and the registry digest is that of the
   index, not a plain image manifest. This exact command was verified against a local `registry:2` (digest
   printed; `release_tools.py`'s label reader returned the version and the 40-hex revision); the push to GHCR
   itself was not. `make docker-build` produces the same image locally if you want to test it first; do not run
   `make docker-test` on it: the image guard correctly fails on the marker (verified: `FAIL  no DEMO-DEFECT marker
   in /app`).
3. Run `demo-unsafe-deploy` once (Actions > demo-unsafe-deploy > Run workflow, branch `main`): `image_digest` =
   the digest, `scenario` = `unhealthy_health`, `confirmation` = `DEPLOY-BROKEN-IMAGE`; approve the `demo-unsafe`
   environment. Leave it running, or let it finish red, and keep the URL as the **fallback recording**. A run
   stays in the `deploy-production` concurrency group until it ends; see the warning in stage 13.

### 2.4 Warm-up and rehearsal (T-30 minutes)

* Warm the Render free instances: `make smoke BASE_URL=https://<service>.onrender.com` (about a minute if they
  were asleep; the smoke test writes one order, harmless because data resets).
* Open the tabs listed in the checklist. Keep terminals at the repo root with `PATH` set.

## 3. Pre-flight checklist

Tick every line before you start (T-10 minutes).

- [ ] `export PATH=$HOME/.local/bin:$PATH`; `uv --version`, `trivy --version`, `k6 version`, `docker info` (or `sudo docker info`) all answer
- [ ] `git status` is empty and `git branch --show-current` is `main`; `make demo-status` prints `baseline: no scenario active`; no `DEMO-DEFECT` in `app/` (`grep -rn DEMO-DEFECT app pyproject.toml uv.lock Dockerfile` prints nothing)
- [ ] `make demo-fixtures` ended with `all fixtures valid.` (needs network)
- [ ] `make quality` is green (or you ran it today on this commit)
- [ ] `make demo-list` shows 13 rows
- [ ] Production Render service is awake: `curl -s https://<service>.onrender.com/api/version` shows version 0.1.0 and release A's commit sha; the dashboard loads
- [ ] Demo Render service is awake and its two fault variables are set; its `/api/version` shows a good image
- [ ] Branch protection shows exactly one required check, `quality-gate`; environments `production` and `demo-unsafe` have reviewers; the reviewer is signed in and reachable
- [ ] Release A digest and sha written down; digest of the broken demo image written down
- [ ] PRs `demo/bad_unit_test` and `demo/bad_security` are open and red; the `demo-unsafe-deploy` run exists (red or in progress)
- [ ] Local branch `demo/bad_lint` exists, **not pushed**; `git log demo/bad_lint --oneline` shows two commits above `main`
- [ ] Browser tabs: repo > Actions, the PR list, a package page `github.com/users/<owner>/packages/container/package/stockflow`, Render dashboard (services, Events), the production dashboard, the demo service dashboard
- [ ] `docker login ghcr.io` is not needed during the demo (the broken image is already pushed); the PAT is not on screen
- [ ] Screen sharing hides notifications, tokens and the Render deploy hook URL (never open Settings > Deploy Hook on screen)
- [ ] You know which fallback applies to each stage (section 7)

## 4. Timeline (35 to 45 minutes)

Total 45 minutes with the optional stage 12 and the dependency interlude; the **core 35-minute cut** drops
stage 12, the dependency interlude in stage 5, and shortens stages 8 and 9 to one minute each.

| Clock | Stage | Minutes | What is on screen |
|---|---|---|---|
| 0:00 | **1 Baseline** | 3 | Production dashboard (note the non-persistent data banner), `/api/version`, green `main` Actions history, `make demo-list` |
| 0:03 | **2 PR creation** | 2 | `git push -u origin demo/bad_lint`, open PR; CI starts; the checks list |
| 0:05 | **3 Lint error** | 3 | `lint-format` fails first (usually the quickest job); log shows `F401`; `quality-gate` red, merge blocked |
| 0:08 | **4 Failed unit test** | 3 | Pre-staged PR `demo/bad_unit_test`: `unit-tests`, `integration-tests`, `coverage` red |
| 0:11 | **5 Security vulnerability detection** | 3 (+3 optional) | Pre-staged PR `demo/bad_security`: `bandit` red (B602). Optional local interlude: `bad_dependency` with pip-audit and Trivy |
| 0:14 (0:17) | **6 Fixing** | 2 | `git revert --no-edit HEAD && git push` on `demo/bad_lint`; CI re-runs |
| 0:16 (0:19) | *buffer while CI runs* | 0 to 3 | Local `make docker-test` output / the PR's `docker-build` job; answer questions |
| 0:19 (0:22) | **7 Successful merge** | 2 | All green, `quality-gate` green, merge; `release` starts |
| 0:21 (0:24) | **8 Docker build** | 3 | `build-scan` job of the pre-staged release A run (what the live run is doing now) |
| 0:24 (0:27) | **9 GHCR publication** | 3 | Package page: tags `sha-<sha>` and `0.1.x`, digest; `docker buildx imagetools inspect` |
| 0:27 (0:30) | **10 Render deployment** | 3 | `deploy` job approval, deploy hook (never shown), Render Events, `/api/version` flipping |
| 0:30 (0:33) | **11 Post-deploy smoke** | 2 | Smoke step output; `make smoke BASE_URL=... EXPECT_SHA=...` from the laptop |
| 0:32 (0:35) | **12 Performance regression (optional)** | 3 | Local k6: green baseline, then `slow_api` crossing the p95 threshold |
| 0:35 (0:38) | **13 Controlled failed deployment** | 4 | `demo-unsafe-deploy` (pre-staged red run, or live approval), Render keeps the old instance serving |
| 0:39 (0:42) | **14 Manual rollback by digest** | 3 | `rollback.yml` with release A's digest; `/api/version` returns to A's sha |
| 0:42 (0:45) | Wrap-up and questions | 3 | Limitations slide: non-persistent data, no auto-rollback, what was and was not verified |

Clock times in parentheses are for the version that includes the optional items. If the live release run
(started at stage 7) finishes during stages 10 to 11, show it: that is the strongest moment of the demo, the
sha on the dashboard changing to the commit you just merged. If it has not, show release A's run (pre-staged)
and return to the live one at the end.

## 5. The 14 stages

Format of every stage: **Do** (exact commands), **Expect** (what should be visible), **Failing job**, **Root
cause**, **Fix**, **Say**, **If it goes wrong**.

### Stage 1: Baseline (3 min)

**Do**

```bash
git switch main && git status --short          # empty
make demo-list                                  # shows the 13 scenarios (output in the README)
make demo-status                                # baseline: no scenario active
curl -s https://<service>.onrender.com/api/version
```

Show: the production dashboard (version and commit in header and footer, the amber banner "Demo data is not
persistent. Products and orders live in memory only and reset whenever the service restarts or redeploys."),
the Actions tab with green runs on `main`, and branch protection with the single required check.

**Expect**

```
$ make demo-status
uv run python -m demo status
[demo] baseline: no scenario active
$ curl -s https://<service>.onrender.com/api/version
{"name":"stockflow","version":"0.1.0","git_sha":"<release A sha>","environment":"production","build_time":"...","repository":"memory","persistent":false}
```

(The `/api/version` shape was verified locally; the production values are what to expect, not observed.)

**Say** "A small FastAPI inventory app. Everything you will see fail is a real tool failing on a real change.
One required check, `quality-gate`, protects `main`; it depends on eleven jobs. The data is in memory by design,
which is why a deploy resets it."

**If it goes wrong** Render dashboard says the service is asleep: wait about a minute (free tier), or fall back
to a local run `make run` and say so. Production shows an older version than expected: continue, the sha in
`/api/version` is your reference for stage 10.

### Stage 2: PR creation (2 min)

**Do**

```bash
git switch demo/bad_lint
git log --oneline -3                            # "demo: bad_lint", "chore: bump version to 0.1.1", main
git push -u origin demo/bad_lint
```

Open the "Compare & pull request" banner, title `Bump version to 0.1.1`, create the PR into `main`. Point at
the checks list: `config-validation`, `lint-format`, `typecheck`, `unit-tests`, `integration-tests`, `coverage`,
`bandit`, `dependency-audit`, `trivy-fs-scan`, `docker-build`, `trivy-image-scan` and `quality-gate`.

**Expect** The PR shows all workflow jobs starting; merging is blocked ("Required statuses must pass") until
`quality-gate` is green. The branch name `demo/bad_lint` is deliberate (the guard test allows defect markers on
`demo/*` branches only).

**Say** "I am not telling CI what is wrong. It will find it. The convention `demo/<scenario>` only exists so the
repository's own anti-defect guard does not fire before the real gate does."

**If it goes wrong** `git push` rejected or SSH not set up: push over HTTPS with a token, or open the
pre-pushed PR you made as a spare in 2.2. PR created but no checks: Actions disabled or workflow permissions
wrong ([SETUP_GITHUB_RENDER.md](SETUP_GITHUB_RENDER.md) 1.2).

### Stage 3: Lint error (3 min), scenario `bad_lint`

**Do** Open the failing `lint-format` job log. To show the same failure locally (use a scratch branch or inject
and reset right away; the engine is reversible):

```bash
make demo SCENARIO=bad_lint
make lint
make demo-reset
```

**Expect** (verified locally; CI runs the same `make lint`)

```
$ make demo SCENARIO=bad_lint
uv run python demo/inject.py bad_lint
[demo] inject bad_lint: injected
[demo]   app/services/orders.py
$ make lint
uv run ruff check .
F401 [*] `os` imported but unused
 --> app/services/orders.py:5:8
  |
5 | import os  # DEMO-DEFECT:bad_lint
  |        ^^
  ...
Found 1 error.
[*] 1 fixable with the `--fix` option.
make: *** [Makefile:84: lint] Error 1
```

**Failing job** `lint-format` (step "ruff check"). `quality-gate` is red because a needed job failed. The
`docker-build` job is also red (image guard: a `DEMO-DEFECT` marker is inside `app/`) and `trivy-image-scan` is
skipped; mention it so nobody is surprised. The other quality jobs stay green.

**Root cause** `app/services/orders.py` imports `os` and never uses it (ruff rule F401).
**Fix** Delete the import (stage 6).
**Say** "Lint is the cheapest gate: seconds, and it runs first. The merge button is disabled by branch
protection, not by anyone's goodwill."
**If it goes wrong** The job is still queued: show the pre-staged PR for `demo/bad_unit_test` instead and
return later. A local run fails with `make: *** [...] Error 1` while ruff itself exits 1; that is normal (make
reports 2).

### Stage 4: Failed unit test (3 min), scenario `bad_unit_test`

**Do** Show the pre-staged PR `demo/bad_unit_test`, open the `unit-tests` job. Locally:

```bash
make demo SCENARIO=bad_unit_test
uv run pytest -q tests/unit
make demo-reset
```

**Expect** (verified; failure counts are from one run)

```
FAILED tests/unit/test_pricing.py::test_golden_vectors[100.00-5.00-7.60-102.60]
FAILED tests/unit/test_pricing.py::test_calculate_total_adds_tax_and_subtracts_discount
...
17 failed, 173 passed in 0.63s
```

and for `make test-integration`: `24 failed, 780 passed, 422 deselected`.

**Failing jobs** `unit-tests` (17 failures), `integration-tests` (24, order totals through the service and
API) and `coverage` (the full suite). Lint, types, bandit stay green: only the tests see this.
**Root cause** `calculate_total` in `app/services/pricing.py` returns `subtotal + tax` instead of
`subtotal - discount + tax`: customers would be charged without their discount.
**Fix** Restore `return subtotal - discount + tax` (revert the demo commit).
**Say** "Types and lint are perfectly happy; only behaviour tests catch this. Note which job names fail and
that three of them go red: the same logic is checked at unit and integration level. And remember this one:
it would pass `/health`, so only these tests stand between this bug and production."
**If it goes wrong** Pre-staged PR missing: run the local commands above (they take seconds with a warm
cache) and show the output.

### Stage 5: Security vulnerability detection (3 min, +3 optional), scenarios `bad_security` and `bad_dependency`

**Do (pre-staged PR)** Show `demo/bad_security`, open the `bandit` job. Locally:

```bash
make demo SCENARIO=bad_security
make security-bandit
make demo-reset
```

**Expect** (verified)

```
>> Issue: [B602:subprocess_popen_with_shell_equals_true] subprocess call with shell=True identified, security issue.
   Severity: High   Confidence: High
   CWE: CWE-78 (https://cwe.mitre.org/data/definitions/78.html)
   Location: app/services/metrics.py:33:11
33	    return subprocess.check_output(f"git rev-parse --short {ref}", shell=True, text=True).strip()
...
make: *** [Makefile:111: security-bandit] Error 1
```

**Failing job** `bandit` (plus `docker-build` through the image guard, as in stage 3). Lint, types and tests
pass: the code is clean and works.
**Root cause** A helper builds a shell command from an f-string (command injection, CWE-78).
**Fix** Delete the helper, or call `subprocess.run(["git", "rev-parse", "--short", ref], check=True)` with an
argument list and no shell (and validate `ref`).

**Optional interlude, `bad_dependency` (dependency vulnerability; needs network)**

```bash
make demo SCENARIO=bad_dependency
make security-audit        # pip-audit on the exported lock
make security-trivy-fs     # trivy fs
make lock-check            # still consistent: a real, valid lock was patched
make demo-reset
```

Expected (verified, trimmed):

```
python-multipart 0.0.6   PYSEC-2026-1850 0.0.7
python-multipart 0.0.6   PYSEC-2026-3039 0.0.27
...
make: *** [Makefile:115: security-audit] Error 1

uv.lock (uv)
Total: 5 (HIGH: 5, CRITICAL: 0)
│ python-multipart │ CVE-2024-24762 │ HIGH │ fixed │ 0.0.6 │ 0.0.7 │ ... │
│                  │ CVE-2024-53981 │      │       │       │ 0.0.18 │ ... │
...
uv lock --check
Resolved 66 packages in 4ms
```

Failing jobs in CI: `dependency-audit` and `trivy-fs-scan` (and the image scan would also see the package;
not run here). Root cause: a direct dependency pinned to `python-multipart==0.0.6`, with a consistent lock.
Fix: remove the pin (or raise it to a fixed release), `uv lock`, commit both files; never add an ignore without
id, reason and expiry.

**Say** "Two different security tools, two different questions: Bandit reads *our* code, pip-audit and Trivy
read *our dependencies* against advisory databases fetched at run time. A green run can turn red tomorrow
without a code change; that is intended."
**If it goes wrong** `bad_dependency` offline or fixture stale: skip the interlude (the check in
`make demo-fixtures` told you beforehand).

### Stage 6: Fixing (2 min)

**Do** On the live PR branch:

```bash
git switch demo/bad_lint
git revert --no-edit HEAD          # reverts "demo: bad_lint"; version bump stays
make lint                          # All checks passed!
git push
```

Verified locally on a scratch copy: after the revert `make lint` prints `All checks passed!`. If the engine
state is still active on your machine, run `make demo-reset` afterwards (verified: it prints `restored
baseline` and leaves `git status` empty). The CI run on the PR restarts (older run cancelled by the
`cancel-in-progress` concurrency of `ci.yml`).

**Expect** The PR's checks reset and start again. Locally nothing contains `DEMO-DEFECT`:

```
$ grep -rn DEMO-DEFECT app pyproject.toml uv.lock Dockerfile ; echo $?
1
```

**Say** "The fix is an ordinary commit: a revert. History keeps the story, and the PR is now a legitimate
change, a version bump."
**If it goes wrong** Revert conflicts (someone else pushed): `git revert --abort`, then `make demo-reset` while
the engine state exists, or delete the `import os` line by hand and `git commit -am "fix: remove unused import"`.

### Stage 7: Successful merge (2 min)

**Do** Wait for `quality-gate` green, then **Merge pull request** (squash or merge commit both work).

**Expect** `quality-gate` green with the job summary table (`## Quality gate`, every job `PASS: success`); the
merge button turns green. After merging, the `ci` push run and `release` start on `main`.

**Say** "Merge is allowed only when one check is green, and that check is green only if all eleven ran and
succeeded. Skipped counts as failed."
**If it goes wrong** CI is still running: spend the time on the buffer (stage 8 explanation using the PR's
`docker-build` job log). If it is red for a reason you did not plant (for example a stale vulnerability
advisory that appeared since the last run), see section 7; never bypass protection or add an ignore live.

### Stage 8: Docker build (3 min)

**Do** In `release` open job `build-scan` (the live one if it has reached it, else the pre-staged run of release
A). Locally, the same steps:

```bash
make docker-test          # build linux/amd64, run, smoke test, image guard, graceful stop (verified, trimmed below)
```

**Expect** (verified; `IMAGE=stockflow:docs` in a scratch copy)

```
== start stockflow:docs (linux/amd64) on host port 49729, container PORT=9123
health status: healthy
[PASS] version: version 0.1.0 sha f4a42669891d env production (matches --expect-sha)
SMOKE TEST PASSED
== image guard
PASS  runs as a non-root user (uid 10001)
PASS  no DEMO-DEFECT marker in /app
PASS  no package installers or build tools (uv, pip, gcc, make, git)
...
stopped in 3s with exit code 0
docker-test PASSED: stockflow:docs
```

Explain: build args bake `GIT_SHA`, version and time into the image and its OCI labels; the image runs as uid
10001; Trivy scans the **exact image** that is later pushed (saved as a tarball, loaded by the `publish` job).
Bonus (verified): `bad_container` shows the image gate: `make demo SCENARIO=bad_container`, `make docker-build`,
`make docker-save`, `make trivy-image-tar` reports 89 vulnerabilities for `debian 12.2` and fails; `make
demo-reset` restores the Dockerfile.
**Say** "Reproducible: both base images are pinned by tag and digest, dependencies come only from the lock.
We test what we ship."
**If it goes wrong** The amd64 build is slow on an arm64 laptop (emulation): use the pre-run output and the CI
log instead of building live.

### Stage 9: GHCR publication (3 min)

**Do** In the `publish` job: the steps "Tag and push", "Capture the digest the registry reports", "Verify what
the registry serves for that digest". Then the package page. Optionally from a terminal:

```bash
docker buildx imagetools inspect ghcr.io/sixseven-pranav-jothivel/stockflow:0.1.1 --format '{{json .Image}}' | head -c 300
```

**Expect** Two tags for one image, `sha-<40 hex>` and the version, both resolving to the same digest (the job
fails if they differ); `release-record` artifact with the digest. `inspect` (as the workflow does it) prints
`version=` and `git_sha=` read from the OCI labels (verified against a local registry: `('0.1.0', '<40 hex>')`).
**Say** "Tags are convenience; **the digest is the identity**. Render is told to deploy a digest, never a tag
that could move. Only the `publish` job has `packages: write`, and what it pushes is the image that passed
Trivy."
**If it goes wrong** `publish` fails with denied/403: package access (SETUP 1.6 step 3). Package page shows
private: make it public, otherwise Render cannot pull.

### Stage 10: Render deployment (3 min)

**Do** In the `deploy` job: approve the `production` environment (required reviewer), then read the step
"Trigger the Render deploy for the exact digest". In Render open the service's Events tab. Do not open the
Deploy Hook settings on screen.

**Expect** Preflight passes (hook secret and `STOCKFLOW_BASE_URL` present; if either is missing the job fails
red with "Deployment is mandatory but not configured"), then
`deploy hook accepted the request (HTTP 200). Accepted is not deployed: waiting for evidence.` (text from the
script; with `RENDER_API_KEY`/`RENDER_SERVICE_ID` it then waits for Render to report the deploy `live`). After
the smoke test the step "Assert release record == digest sent to Render == what is serving" runs
`release_tools.py verify-deployment`; only when it also passes is the run green ("deployed and verified"). Render
shows a new deploy for `ghcr.io/sixseven-pranav-jothivel/stockflow@sha256:...`; the old instance serves until
the new one passes `/health`; `/api/version` flips to the new version `0.1.1` and the merged commit's sha.
All of this is expected from the scripts and Render's docs, not observed.
**Say** "A 200 from the hook means *accepted*. The deployment is proven by the version endpoint reporting the
commit we just merged. Render keeps the old instance until the new one is healthy. The data you saw earlier
is gone: the service restarted."
**If it goes wrong** `deploy` fails with `Deployment is mandatory but not configured` (hook secret or base URL
missing): the image is already in GHCR; fix the configuration and use *Re-run failed jobs* on the run (no
rebuild) or run `release` via workflow_dispatch. Say plainly that a release is never green without a verified
deployment. Hook returns 404/400: `imgURL` does not match the service's image URL
(SETUP 3.1). Free instance asleep: first request takes about a minute.

### Stage 11: Post-deploy smoke test (2 min)

**Do** Show the smoke step of the `deploy` job (`python3 scripts/smoke_test.py --base-url "$BASE_URL"
--expect-sha "$GITHUB_SHA" --timeout "$SMOKE_TIMEOUT"`), then from your laptop:

```bash
make smoke BASE_URL=https://<service>.onrender.com EXPECT_SHA=<merged commit sha>
```

**Expect** (format verified locally and against the container; production values are what to expect)

```
StockFlow smoke test against https://<service>.onrender.com
expecting git sha <merged commit sha>
[PASS] health: status ok
[PASS] ready: ready (repository: memory)
[PASS] version: version 0.1.1 sha <12 hex> env production (matches --expect-sha)
[PASS] products: 10 product(s)
[PASS] order: order #1 for WID-001 total 10.79 round-tripped
[PASS] dashboard: dashboard rendered
SMOKE TEST PASSED
```

Then the `deployment-record` artifact (`deployed: true`, digest, sha, outcome). The smoke test and the `verify-deployment` assertion must both pass before the run is reported as deployed and verified.
**Say** "The smoke test refuses to pass while an old version is still serving: it retries with backoff until
`/api/version` reports the expected sha. It also places one real order and checks the arithmetic."
**If it goes wrong** `git_sha mismatch ... (old version still serving, or the wrong build was deployed)`: the
deploy has not finished; wait or check Render Events. Remember the test writes an order; it resets on the next
deploy.

### Stage 12: Performance regression (optional, 3 min), scenario `slow_api`

Runs **locally** (verified) so the free Render instance is not hammered and nothing writes to the audience's
data. Two terminals.

**Do**

```bash
# terminal 1: baseline
PORT=8123 uv run python -m app &
make perf BASE_URL=http://127.0.0.1:8123          # 30 s, 10 VUs; thresholds from the policy
kill %1

# terminal 1: inject the fault (merging the patch alone changes nothing: it needs two env keys)
make demo SCENARIO=slow_api DEMO_ARGS=--allow-runtime-failures
STOCKFLOW_DEMO_FAULT=slow_api STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true PORT=8123 uv run python -m app &
make perf BASE_URL=http://127.0.0.1:8123
kill %1; make demo-reset
```

**Expect** (verified)

```
# baseline
    ✓ 'p(95)<500' p(95)=6.77ms
    ✓ 'rate<0.01' rate=0.00%                                  (http_req_failed)
perf rc=0

# with slow_api armed
    http_req_duration
    ✗ 'p(95)<500' p(95)=765.73ms
    http_req_failed
    ✓ 'rate<0.01' rate=0.00%
time="..." level=error msg="thresholds on metrics 'http_req_duration' have been crossed"
make: *** [Makefile:168: perf] Error 99
```

and with the fault code present but no env keys the endpoint is fast (`GET /api/products` 0.022 s) while
armed it takes about 0.79 s (verified with curl).

**Failing job** None in PR CI: all PR gates pass on purpose, because the defect is inert there. The failing
signal is k6 (`perf.yml` against a deployed instance, or the command above). **Root cause** `GET /api/products`
sleeps 750 ms only when both `STOCKFLOW_DEMO_FAULT=slow_api` and `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true`
are in the process environment. **Fix** Unset the variable or `make demo-reset` / redeploy without the patch.
**Say** "Functionally correct, so every functional test passes. Only a load test with a latency threshold sees
it. The thresholds live in the policy file, not in the test script. The k6 test writes data, so it is opt-in."
**If it goes wrong** k6 missing: `make tools`. Port busy: change `PORT`. Skip the stage (it is optional).

### Stage 13: Controlled failed deployment (4 min), scenarios `unhealthy_health` / `bad_deployment`

Be exact about what this shows. **Render does not roll back.** For a *new instance that never becomes
healthy*, Render's own documentation says: traffic is not sent to it until its health check passes, the
previous instance keeps serving, and after 15 minutes without a healthy new instance Render cancels the deploy
and keeps routing to the existing instance. For a bad build that **does** pass `/health` (a logic defect), Render
puts it live and **only your smoke test notices**; recovery is stage 14. The pipeline never builds the broken
image: **the presenter pushes it manually** (section 2.3) and a separate, manual, reviewer-gated workflow
deploys it.

**Do** Prefer showing the pre-staged run. Live variant:

1. Actions > `demo-unsafe-deploy` > Run workflow on `main`:
   `image_digest = sha256:<digest of the demo-unhealthy-health image>`, `scenario = unhealthy_health`,
   `confirmation = DEPLOY-BROKEN-IMAGE`.
2. Job `guard` (typed confirmation, snake_case scenario id, digest format, the digest exists in GHCR with OCI
   labels) passes; job `deploy-unsafe` waits for the `demo-unsafe` reviewer: approve it. The job summary banner
   reads "DEMONSTRATION: deploying an image that bypassed the quality gates".
3. In Render (demo service) the new deploy appears but never goes live; meanwhile the old instance keeps
   serving: `curl -s https://<demo-service>.onrender.com/api/version` still shows the previous good sha.
4. The workflow's `Verify` step runs the smoke test with `--expect-sha <broken image's sha>` and retries until
   its timeout (the policy's `smoke.timeout_seconds`, which is 10 minutes at the time of writing) while the
   old sha keeps serving, then fails. With `RENDER_API_KEY` and `RENDER_SERVICE_ID` configured, the earlier
   `deploy_render.py` step ends sooner, as soon as Render reports the deploy failed or canceled.

The red result takes minutes: **do not wait for it live.** Show the pre-staged red run and its job summary
and record (`demo-unsafe-record`, `deployed` false or `deployed_unverified`).

**Expect (locally verified building blocks)**

```
# the broken image, run with both keys, as Render would run it
HTTP/1.1 503 Service Unavailable
{"status":"unhealthy"}
$ make docker-guard IMAGE=<that image>
FAIL  no DEMO-DEFECT marker in /app          # the image guard refuses it, so it can never come from the pipeline
# the smoke test against it
[FAIL] health: GET /health: expected HTTP 200, got 503: '{"status":"unhealthy"}' (gave up after 11 attempt(s) / 60s)
SMOKE TEST FAILED: health
# the same code without the two env keys is healthy
[PASS] health: status ok   ... SMOKE TEST PASSED
```

Variant `bad_deployment` (crash at startup, verified):

```
RuntimeError: simulated startup failure (STOCKFLOW_DEMO_FAULT=bad_deployment)
rc=1
```

**Failing job** `deploy-unsafe` (step "Verify (a failure here is the expected, instructive outcome)", or the
deploy step with API credentials). A red run **is** the demonstration.
**Root cause** An image that never became healthy in the target environment; it reached Render because the
demo workflow bypasses the quality gates on purpose. In real life the same failure class arrives through a bad
configuration change or an environment difference.
**Fix / recovery** Nothing is wrong with the serving instance if Render kept the old one (check
`/api/version`). If the broken image went live (for example you pointed the environment at production, or a
logic-defect image passed health), run `rollback.yml` with the previous good digest (stage 14).
**Say** "Three layers: the PR gates never let this image be built by the pipeline; Render's health check stops
a non-starting instance from receiving traffic; and the smoke test refuses to call it a success. What is
*not* automatic is rollback: that decision is a human with a digest."
**WARNING (concurrency)** `demo-unsafe-deploy`, `release` and `rollback` share the concurrency group
`deploy-production` with `cancel-in-progress: false`. A still-running unsafe run **queues** a rollback behind
it (jobs have a 40-minute limit). Before stage 14, let the unsafe run finish or cancel it (Actions > the run >
Cancel workflow run).
**If it goes wrong** Reviewer unavailable or the live run is slow: use the pre-staged run. No second Render
service: target production only if you accept production serving the fault; otherwise present the pre-staged
run and the local evidence above. Remove the two fault variables from the Render service afterwards (section 8).

### Stage 14: Manual rollback by digest (3 min)

**Do** Actions > `rollback` > Run workflow on `main`:

* `image_digest`: release A's digest `sha256:<64 hex>` (from the `release-record` artifact, the release run
  summary, or the package page)
* `reason`: `demo: restore the previous release`

Approve the `production` environment.

**Expect** (from the workflow definition, not observed) job `verify-image` confirms the digest exists in GHCR
and reads version and git sha from its labels; job `rollback` runs `deploy_render.py` with that digest (no
rebuild), then the smoke test with `--expect-sha` set to **the restored image's** sha, then writes
`rollback-record`. Production's `/api/version` returns to release A's version and sha. A rollback that cannot run
(no hook or base URL configured) fails loudly rather than doing nothing.
**Failing job** None expected. **Root cause** n/a: the rollback is the recovery.
**Say** "Rollback is just 'deploy an old digest'. Nothing is rebuilt, so what comes back is bit-for-bit what
ran before. The next push to `main` will deploy forward again; if the cause is not fixed, roll back again."
**If it goes wrong** `verify-image` fails with "image not found": wrong digest or package private. The job
sits in "Queued": the `deploy-production` concurrency group is busy (cancel the unsafe or release run).
Rollback impossible (`RENDER_DEPLOY_HOOK_URL`/`STOCKFLOW_BASE_URL` missing): configure them; there is no
workaround that skips the check. Last resort: in the Render dashboard use manual deploy of the earlier image
reference.

### Optional stage P: per-PR preview environment (3 to 4 min; main paths observed on 2026-10-12, a few paths still NOT VERIFIED)

Only if `pr-preview.yml` has been set up and rehearsed ([SETUP_GITHUB_RENDER.md](SETUP_GITHUB_RENDER.md) section
8). It is not one of the 14 stages and is not in the timeline totals; it fits after stage 7 or in the wrap-up.
The label-then-close path was run for real on 2026-10-12 (PR #3, `pr-preview` run 38209130008, cleanup run 38209469656; see SETUP section 8.0 for what was seen). Also observed on 2026-10-12 (PR #4, real events): a re-push redeploys the existing preview in place (run 38211488688), removing the label deletes it (run 38213110510), and a Dependabot PR's run is skipped. Still NOT VERIFIED: the fork guard on a real fork, the gate timeout and the red-gate "no preview" path, so do not demonstrate those live without a rehearsal.

**Pre-stage** A same-repository branch `feature/preview-demo` with a trivial, legitimate change (for example the
version bump from section 2.2 applied to a different branch) and a green PR. Create the label once. At T-30 add the
`preview` label and let the preview deploy, so there is a real preview to show if the live one is slow. Do **not**
use a `demo/<scenario>` branch: its `quality-gate` is red, so the preview correctly never deploys (show that as a
bonus: the sticky comment reads `No preview` with `quality-gate FAILED for this commit`).

**Do**

1. Open (or show) the PR, add the label `preview` in the sidebar.
2. Actions > `pr-preview`: walk the jobs `decide`, `gate` (waits for `quality-gate` on this exact commit),
   `build-scan`, `publish` (tags `pr-<n>` and `pr-<n>-<sha7>`), `preview`, `report`.
3. Open the sticky comment: status, commit, image digest, preview URL. Open the URL and `/api/version`.
4. Close the PR (or remove the label; both paths were observed). The `cleanup` run starts; the comment changes to `Preview deleted`; in
   Render the service `stockflow-pr-<n>` is gone.

**Expect** (observed on PR #3) comment headline `DEPLOYED and verified` with a URL of the form `https://stockflow-pr-<n>.onrender.com`; `/api/version` serves version `<version>-pr<n>` (for example `0.1.0-pr3`) and the PR head sha; the preview reports environment `preview-pr-<n>` (a build argument set by `pr-preview.yml`; PR #5 served `"environment":"preview-pr-5"`) and its data is its own in-memory data, separate from production. On close, the cleanup run succeeds, the Render service is gone and the comment reads `Preview deleted`. Every closed PR starts a cleanup run.
**Failing job** None expected. Unconfigured: `preview` red with `NOT DEPLOYED: preview not configured`.
**Say** "Same gates, same scanner, deployed by digest, only for a labelled same-repo PR, only after a green gate,
and deleted when the PR closes. Render never deletes image previews itself, so the cleanup job exists."
**If it goes wrong**

| Symptom | Recovery |
|---|---|
| `NOT DEPLOYED: preview not configured` | Secrets missing from the `preview` environment: say so, show the pre-staged preview or skip the stage |
| Preview takes too long or Render rejects it (free plan and the `@sha256` digest form worked on 2026-10-12, but check the job log) | Show the pre-staged result or the comment text; do not wait live; skip the stage |
| `quality-gate FAILED` comment | Expected on a defect PR; use a green PR for the live demo |
| `Preview cleanup FAILED` | The comment names `stockflow-pr-<n>`: delete it in the Render dashboard, say that Render never auto-deletes image previews |
| Reviewer approval pending on `preview` | Approve the environment deployment |

## 6. Remaining scenario cards

Same format; every command and output below was verified locally in a scratch copy and the tree restored with
`make demo-reset` afterwards. "Failing CI job" is mapped from the Make target, not observed on GitHub.

| Scenario | Inject | Show | Expected signature | Failing CI job(s) | Root cause | Fix |
|---|---|---|---|---|---|---|
| `bad_format` | `make demo SCENARIO=bad_format` | `make format-check` | `File would be reformatted` / `app/services/pricing.py`, `1 file would be reformatted, 72 files already formatted`; `make lint` still passes | `lint-format` (step "ruff format --check") | A statement written with single quotes and padding spaces: valid, lint-clean, not formatted | `uv run ruff format .`, commit (or revert) |
| `bad_typecheck` | `make demo SCENARIO=bad_typecheck` | `make typecheck` | `app/services/products.py:19: error: Incompatible return value type (got "Product", expected "str")  [return-value]` and `app/api/products.py:51: ...`, `Found 2 errors in 2 files` | `typecheck` | `get_product` annotated `-> str` while returning a `Product`; tests still pass at runtime | Restore `-> Product` |
| `bad_integration_test` | `make demo SCENARIO=bad_integration_test` | `make test-integration` (fails), `make test-unit` (passes) | `26 failed, 778 passed, 422 deselected`; unit: `360 passed` | `integration-tests`, `coverage` (`unit-tests` stays green) | `place_order` decrements stock by 0 instead of the ordered quantity | Restore `uow.decrement_stock(item.product_id, item.quantity)` |
| `bad_coverage` | `make demo SCENARIO=bad_coverage` | `make test-coverage` | `app/services/legacy_import.py   190   190   68   0   0%`, `FAIL Required test coverage of 85% not reached. Total coverage: 78.70%`; all tests pass | `coverage` | ~190 untested statements added in a module nothing imports | Delete it or cover it with tests; **never lower `fail_under`** |
| `bad_container` | `make demo SCENARIO=bad_container` | `make docker-build && make docker-save && make trivy-image-tar` | `reports/image.tar (debian 12.2)  debian  89` vulnerabilities; non-zero exit; `make demo-fixtures` confirms 92 fixable HIGH/CRITICAL for the pinned old base | `trivy-image-scan` (the image still builds and runs) | Base stage repointed to a 2023 Debian image with fixable HIGH/CRITICAL OS findings | `make demo-reset` and rebuild; image scans block only on findings that have a fix |
| `bad_deployment` | `make demo SCENARIO=bad_deployment DEMO_ARGS=--allow-runtime-failures` | `STOCKFLOW_DEMO_FAULT=bad_deployment STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true uv run python -m app` | `RuntimeError: simulated startup failure (STOCKFLOW_DEMO_FAULT=bad_deployment)`, exit 1 | none in PR CI; on Render the new instance never becomes healthy | `create_app` raises at boot when both env keys are set | Unset the variable on the service, or roll back to the previous digest |

Runtime scenarios and the two-key rule: injecting them requires `DEMO_ARGS=--allow-runtime-failures`
(otherwise the engine refuses), and the injected code does nothing unless the **process environment** has both
`STOCKFLOW_DEMO_FAULT=<id>` and `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true`; it never reads request headers,
query parameters, cookies or bodies (verified: forged inputs do not trigger it, covered by `tests/demo`).

## 7. What if it goes wrong

| Symptom | Likely cause | Fast recovery | Do not |
|---|---|---|---|
| PR checks never appear | Actions disabled; workflow not on the default branch; permissions | Show the pre-staged PRs; fix after the demo (SETUP 1.2) | Edit workflows live |
| A job is red that you did not plant | Advisory database changed since `make demo-fixtures` (a new CVE in a pinned dependency or the base image); a flaky test; a tool pin drifted | Open the log; if it is a new advisory, say it is the gate working, switch to pre-staged results | Add an ignore, lower a threshold, or bypass branch protection live |
| CI is slow | Queue, cold caches, emulation | Use the pre-staged runs; talk through the stage with the pre-staged logs | Wait silently |
| `quality-gate` red but every named job looks green | A needed job was skipped or cancelled (counts as failure) | Look at the `## Quality gate` summary table: it lists every job with PASS or FAIL | |
| `release` never starts after merge | Merge made by a bot token; workflow disabled; push event lost | Run it manually: `release` > Run workflow on `main` | |
| `deploy` waits forever | `production` environment waiting for approval | Click "Review deployments" and approve; make sure a reviewer is available | |
| `deploy` red: `DEPLOYMENT FAILED: not configured (deployment is mandatory)` | Secret `RENDER_DEPLOY_HOOK_URL` or variable `STOCKFLOW_BASE_URL` missing (the image was still published first) | Fix the config, then *Re-run failed jobs* on that run (redeploys the published digest, no rebuild) or run `release` via workflow_dispatch | Claim it deployed; set `require_deploy` false to hide it |
| `deploy` red at "Assert release record == digest sent to Render == what is serving" | `verify-deployment` mismatch (digest/commit/version/name) or Render not `live` | Read the `MISMATCH:` lines; check Render Events and `/api/version` | Re-run blindly |
| Hook returns 404 or 400 | `imgURL` host/owner/repo does not match the Render service's image URL; owner uppercase | Recreate the service with `ghcr.io/sixseven-pranav-jothivel/stockflow:<tag>` (SETUP 3.1) | |
| Hook returns 401 | Hook URL regenerated or copied wrongly | Regenerate the hook in Render, update the secret | Paste the hook into chat |
| Render pull fails | GHCR package still private | Make the package public (SETUP 1.6) | |
| Smoke test: `git_sha mismatch` | Deploy not finished, or the wrong build deployed | Wait, check Render Events; if Render cancelled, the old instance is still serving | |
| Smoke test hangs/times out on first request | Free instance spinning up (about a minute) | Warm it earlier (`make smoke`) | |
| Dashboard data vanished | Normal: every deploy/restart resets in-memory data | Point at the banner | Promise persistence |
| `demo-unsafe-deploy` guard fails | Wrong confirmation text; digest not in GHCR; package private; label missing (image not built with the build args) | Re-run with `DEPLOY-BROKEN-IMAGE`; rebuild with `--build-arg GIT_SHA=$(git rev-parse HEAD)` and `APP_VERSION` | |
| `rollback` queued | Another run holds the `deploy-production` concurrency group | Cancel the unsafe/release run | |
| `make demo` refuses: `ERROR: app/services/pricing.py already contain DEMO-DEFECT: markers but there is no .demo-state/state.json, so the engine cannot restore them` (verified, exit 2) | The target file already carries a defect committed on the current branch (you are on a `demo/*` branch, or switched branches with a scenario committed) | `git switch -f main` (a baseline branch), then inject | Edit files by hand |
| `make demo-reset` exits 2 listing files | You edited a file that the active scenario owns (hash is neither injected nor original) | Save your work; `make demo-reset DEMO_ARGS=--force` (your copy is kept under `.demo-state/discarded/<timestamp>/`) | |
| `make demo-fixtures` fails or exits 3 | Advisory withdrawn/re-scored (exit 1) or offline (exit 3) | Skip that scenario (`bad_dependency` / `bad_container`) | Demo a stale scenario |
| `docker` permission denied | Needs sudo on the dev VM | `make ... DOCKER="sudo docker"` or let the Makefile auto-detect | |
| `make docker-test` fails at "image guard" | An injected scenario is still active | `make demo-reset`, rebuild | |
| k6 not found | Not installed | `make tools` | |
| Everything is on fire | | Stop. Present the verified local outputs in this runbook and the pre-staged runs; state what you could not show | Improvise credentials or settings |

## 8. After the demo

* `make demo-reset` on every machine that injected something; `git switch -f main`; `git status` empty;
  `make demo-status` says `baseline: no scenario active`. (The git-ignored `.demo-state/` directory may remain
  without a `state.json`; that is harmless.)
* Remove the two fault variables from the Render service (`STOCKFLOW_DEMO_FAULT`,
  `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES`); verify with `make smoke`.
* Delete the demo image tag/version from the GHCR package page, delete the `demo/*` branches, close the demo PRs.
* If production still runs the rolled-back image, the next merge to `main` deploys forward; note it.
* Rotate the PAT you used for the manual push; regenerate the Render hook if it was ever on screen.
