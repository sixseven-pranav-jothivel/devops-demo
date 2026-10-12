# StockFlow security

Owner: security agent. Contract: `docs/ARCHITECTURE.md` sections 10-13. Thresholds live **only** in
`config/quality-policy.yaml`; this document explains them, it does not redefine them. Where a number is
quoted below, `python scripts/quality_policy.py get <key>` is authoritative.

Contents (section 10.9 reviews the PR preview workflow): 1 Principles - 2 Threat model - 3 Action pinning - 4 Workflow permissions - 5 Secrets inventory -
6 Scanner policy - 7 Exceptions - 8 Known limitations - 9 Verified commands - 10 DevOps hand-off -
11 Backend and demo requirements - 12 Design note for `tests/test_quality_policy.py`

---------------------------------------------------------------------------------------------------

## 1. Principles

1. One policy file, no duplicated numbers (`scripts/quality_policy.py get|args|check`).
2. A gate is never weakened to pass; the code is fixed. `check` enforces policy *floors* (`FLOORS` in the
   script), so lowering a threshold requires a deliberate, reviewed edit in two places.
3. Every exception carries **id + reason + expiry** and fails CI when it expires.
4. The clean baseline passes every gate; injected demo defects are caught by the real tools.
5. Least privilege everywhere (tokens, secrets, image contents); fail closed.
6. No secrets in the repo, the image, logs, or job summaries.

---------------------------------------------------------------------------------------------------

## 2. Threat model

Assets: the `main` branch and its release pipeline; the published image `ghcr.io/<owner>/stockflow`; the
Render service and its deploy hook; GitHub tokens; the contributors' trust in CI results.
Actors: anonymous internet users (reach the app and the public image), fork-PR authors (untrusted code in
CI), compromised third-party dependencies/actions/base images, a careless maintainer.
Non-goals: the app holds no personal data, no credentials, and no persistent data (in-memory, reset on
restart). Authentication is out of scope for this demo (see 8).

| # | Threat | Vector | Mitigations (control) | Residual risk |
|---|---|---|---|---|
| T1 | Supply chain: malicious/vulnerable Python dependency | PyPI package or transitive dep | `uv.lock` committed, `uv sync --frozen` in CI and image; pip-audit (any vuln fails); Trivy fs on `uv.lock` (HIGH/CRITICAL); Dependabot-style update PRs reviewed; image contains runtime deps only | Zero-day or typosquat not yet in advisory DBs; hashes are not enforced (see 8) |
| T2 | Supply chain: compromised GitHub Action (tag re-pointed) | mutable `@v4` tag | Full 40-hex SHA pinning verified through the GitHub API (3); `check` fails on an unpinned `uses:`; minimal permissions (4); prefer `run:` with pinned CLI over marketplace wrappers | A pinned SHA can itself be malicious if the pin was wrong; verify at pin time |
| T3 | Supply chain: vulnerable or swapped base image | `python:3.12-slim` tag moves | `Dockerfile` pins both bases by tag **and** digest (`python:3.12-slim@sha256:...`, `ghcr.io/astral-sh/uv:0.12.24@sha256:...`); Dependabot (docker ecosystem) proposes digest refreshes; Trivy image scan gates every PR and release; the image is built by us and deployed by **digest**; runtime stage has no pip, uv, compilers or git (image guard) | Unfixed OS CVEs are tolerated by policy (7.2) but reported in full |
| T4 | Secret leakage into repo/image/logs | commit, build arg, `set -x`, error messages | No secrets in app (`app/config.py` has none); `.gitignore` has `.env`; allow-list `.dockerignore`; Trivy secret scanner on fs and image; deploy script never prints the hook URL and redacts it from exceptions; no secret as `ARG`/`ENV`; GitHub masks secrets | A secret pasted into an issue/PR comment is outside the pipeline |
| T5 | Fork PR runs attacker code with our secrets | `pull_request_target`, `secrets: inherit`, workflow_run chaining | `ci.yml` triggers on `pull_request` only (no secrets, read-only token for forks); `pull_request_target` banned by `check`; `secrets:` never forwarded to ci.yml; release/rollback cannot be triggered by a PR; no PR-derived text interpolated into `run:` (script injection) | Maintainer approving a malicious fork run; mitigate with the repo setting "require approval for all outside collaborators" |
| T6 | Deploy hook / Render API credential abuse | leaked `RENDER_DEPLOY_HOOK_URL` lets anyone deploy an arbitrary `imgURL`; leaked `RENDER_API_KEY` | Both are **environment secrets** (`production`, `demo-unsafe`), read only by the single `deploy_render.py` step of a job that declares `environment:` (enforced by `check`); `ci.yml`, `perf.yml`, `demo-fixtures.yml` must be secret-free (enforced); `deploy_render.py` validates `ghcr.io/<owner>/stockflow@sha256:<64hex>` (tags refused), HTTPS only, masks the hook and each derived value with `::add-mask::`, redacts exceptions, never uses a CLI argument for secrets; the API key is only sent to `https://api.render.com` and used for GET requests; rotation = regenerate hook / revoke key in Render, update the secret; Render auto-deploy is off | Anyone with the hook URL can redeploy until rotated; **Render API keys are not scoped**: the key can do everything its owner can, so "read-only polling" is a code convention, not a Render-enforced limit (8) |
| T7 | GHCR package is public | anyone can pull the image | Intended (Render pulls without credentials). Image holds only `app/`, `pyproject.toml`, `uv.lock` (allow-list), no secrets, no `demo/`, `tests/`, `.github/`; image guard + Trivy secret scan fail the release otherwise; package is written only with the job-scoped `GITHUB_TOKEN` (`packages: write` on `push` only) | Public code and dependency list are visible by design |
| T8 | Demo fault reaches production | `slow_api`, `unhealthy_health`, `bad_deployment` patches merged | Baseline has no fault hooks; the injected code is **two-key**: active only if the process env has BOTH `STOCKFLOW_DEMO_FAULT=<scenario id>` and `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true`, read from `os.environ` only, **never** from request data (the demo verifier replays forged headers/query strings and one-key runs); the engine refuses to inject runtime scenarios unless `demo.allow_runtime_failures` or `--allow-runtime-failures` is set; Render never sets either variable; `DEMO-DEFECT` baseline test + image guard fail a build that contains a marker; `demo/` never enters the image | An operator can set both variables on Render deliberately (that *is* the demo) |
| T9 | Request-header / request-data injection | attacker-controlled `Host`, `X-Forwarded-*`, `X-Request-ID`, `Content-Type`, query | No behaviour or fault is selected by headers/query/body/cookies; no header value is reflected into a response header or log line without validation (CRLF/log injection); if a request id is accepted it must match `^[A-Za-z0-9._-]{1,64}$` else a server-generated UUID is used; `Location` headers are built from server-side ids only; Pydantic v2 validates all bodies and path/query types; SQL only via SQLAlchemy bound parameters (bandit B608 would flag string-built SQL); Jinja2 autoescape on; error handlers never echo stack traces; uvicorn runs behind Render's proxy | Missing HSTS/CSP headers are Render/edge concerns for this demo (8) |
| T10 | Over-privileged workflow token | default `write-all` | Top-level `permissions: {contents: read}` in every workflow (enforced by `check`); write permissions only per job and only those allow-listed in the policy (4) | Repo-level default token setting must also be "read" (documentation agent lists the setting) |
| T11 | Weakening a gate quietly | edit a threshold, `continue-on-error`, `\|\| true`, `--exit-zero`, `--exit-code 0` | `check` floors and a drift scan of Makefile/workflows/`scripts/deploy` fail on literal gate flags and on swallowed failures; branch protection requires only `quality-gate`, which `needs:` every job | A maintainer with admin rights can still disable branch protection |
| T12 | Tampering with the deployment record or rollback target | wrong digest | Rollback input must match `sha256:[0-9a-f]{64}`, existence verified with `docker manifest inspect`, rollback never rebuilds; digest is the identity in the record | Record is a workflow artifact (90 days), not tamper-evident storage |
| T13 | A defective image reaches Render deliberately (controlled failed deployment demo) | presenter builds a scenario image and pushes it to GHCR by hand, then runs `demo-unsafe-deploy.yml` | `release.yml` can never publish a defective build (all gates + Trivy on the exact image precede `publish`, and baseline guard rejects `DEMO-DEFECT` markers); the demo path is separate: `workflow_dispatch` only, main-only, exact typed confirmation `DEPLOY-BROKEN-IMAGE`, snake_case scenario id validation, digest must already exist in GHCR, runs in environment `demo-unsafe` (configure **required reviewers**; prefer its own hook secret and `STOCKFLOW_BASE_URL` pointing at a separate demo service); the manual push is made with the presenter's own credentials, never by CI; recovery is `rollback.yml` by digest | **Accepted risk**: if `demo-unsafe` shares the production hook, production serves the broken image until rollback; a presenter PAT with `write:packages` exists on a workstation; the manually pushed demo image sits in the public GHCR package under a sha/demo tag (delete the version after the demo) |
| T14 | Poisoned build cache or artifact hand-off | `actions/cache` (uv, Trivy DB), the image tarball and the `release-record` artifact passed between jobs | Caches are written only by branch-scoped runs (PR caches cannot be read by `main`); the tarball is produced and consumed inside one run (retention 1 day) and `publish` verifies its sha256 (reported by `build-scan` as a job output) before `docker load`; `publish` pushes those bytes and then verifies the registry labels name this commit; `deploy` downloads `release-record` by **name only** (no `run-id`, no `github-token`), which `download-artifact` resolves to artifacts of the current run, and uses it only as a cross-check (`verify-deployment`), never as the source of the image to deploy (that comes from the `publish` job output) | A cache written by a compromised `main` run could hide advisory-DB entries; the artifact service is trusted within a run |
| T15 | PR preview: workspace-wide Render API key reachable from PR-controlled workflow | `pr-preview.yml` runs on `pull_request`, so GitHub uses the workflow file **from the PR merge commit**: anyone who can push a branch to this repository can edit the `preview` / `cleanup` job and print or send `RENDER_API_KEY` | No `pull_request_target`; forks and Dependabot get no preview (job-level `if` plus `preview_lib.decide`, deny by default); secrets exist only in environment `preview`, are exposed as `env:` of one step, and `build-scan` (the job that runs PR code) has none; privileged jobs check out the **base** branch and run base scripts; the key is only sent to `https://api.render.com` without redirects; scripts can delete only a service named `stockflow-pr-<n>` and refuse the base service | **Not preventable inside the workflow file; and currently open (see 5 and 10.10): the `preview` environment has no reviewers and no deployment-branch rule, and its key equals production's.** The environment protection rules are the control (6, 8): dedicated Render workspace and user so the key cannot reach production, required reviewers with self-review prevented, deployment-branch rule `refs/pull/*/merge`. Writers are trusted by design |
| T16 | Authorization and gate bypass for previews | `labeled` by anyone with triage or write; `quality-gate` check spoofed; label stays on the PR when new commits are pushed | Only same-repo, non-Dependabot PRs with the `preview` label; label events need triage/write rights (a non-collaborator cannot label); `wait-gate` accepts only a `quality-gate` check run created by the GitHub Actions app for the exact head sha; `build-scan`, Trivy and smoke run before anything is published | The label is not a security boundary against write users (they can label their own PR), and the gate is **not authentication**: any workflow on the same commit that defines a job named `quality-gate`, including one edited in the PR, satisfies it. Later pushes to a labelled PR are deployed without a new human decision |
| T17 | Preview lifecycle races leave live previews | originally: an update event could cancel a queued or running cleanup, and `closed` only cleaned up when the label was still on the PR; API outage during cleanup | **Fixed in code (commit e0f42d8), unit-tested, real-event behaviour NOT VERIFIED.** `preview_lib.decide`: `closed` always cleans up (deleting is idempotent); cleanup events (`closed`, `unlabeled`) run in their own concurrency group `pr-preview-<n>-cleanup` with `cancel-in-progress: false`, so no update can cancel them; the `preview` job runs `preview_tools.py recheck --phase before` and `--phase after` (GitHub API `GET /pulls/<n>`, job permission `pull-requests: read`): if the PR was closed or unlabelled it does not deploy, or deletes the preview it just created; if a newer commit superseded it, it stops and leaves the preview to the newer run; failing to ask the API fails the job; `report` states `cleanup_failed` | Residual: the race branches (unlabel then push, closed after unlabel, supersede during deploy) have been exercised by unit tests only (`tests/deploy/test_preview.py`, `test_pr_preview_workflow.py`; 276 deploy tests), not by real GitHub events; only the plain `closed` path ran for real (cleanup run 38209469656, label still set). A failed or timed-out Render API call during cleanup still leaves a preview (reported, not retried). **Side effect:** every closed same-repo, non-Dependabot PR now starts a `cleanup` job in environment `preview`, whether or not it ever had a preview (an extra Render API call with the account-wide key per closed PR; with required reviewers each one would wait for approval) |
| T18 | GHCR `pr-<n>` tags accumulate and share the production package | every `synchronize` pushes `pr-<n>` and `pr-<n>-<sha7>` to `ghcr.io/<owner>/stockflow`; nothing deletes them; Render requires the preview image path to match the base service's | `publish` pushes only after the sha256 check and only tags derived from the PR number and sha7; production and rollback deploy **by digest**, never by tag; the release/rollback paths validate `sha256:` digests and OCI labels | Package clutter and storage use; a write user can overwrite mutable tags such as `<version>` through any workflow with `packages: write` (this already held before previews); `render.yaml`'s default tag must not be relied on for production |
| T19 | Preview inherits base-service settings; parts of the Render preview flow not yet exercised | `POST /v1/services/{id}/preview` copies the base service settings including env vars | The base service carries no secrets (`DEPLOY_ENVIRONMENT`, `REPOSITORY_BACKEND` only); previews are deployed by digest and smoke-tested for the head sha. **Observed 2026-10-12** (PR #3, label `preview`, run 38209130008 deployed, run 38209469656 cleaned up): the preview was created and served the PR head sha, it ran on the Render **free** plan, `imagePath` accepts the `ghcr.io/<owner>/stockflow@sha256:<digest>` form, and it inherited the base env vars (it reported `DEPLOY_ENVIRONMENT=production`) | The preview reports `DEPLOY_ENVIRONMENT=production`. **Never add secrets to the base service's environment variables**: PR code would receive them in the preview. NOT VERIFIED: paid-plan behaviour (`PREVIEW_PLAN` set), redeploy of an existing preview on `synchronize`, the cleanup race of T17, and behaviour when the Render API is unavailable |

---------------------------------------------------------------------------------------------------

## 3. GitHub Actions pinning policy

Policy keys: `actions.pin_to_full_sha`. Enforced by `scripts/quality_policy.py check` (every non-local
`uses:` must end in `@<40 hex>`; `docker://` refs need `@sha256:`).

Rules
- Pin every third-party action (and every `actions/*` action) to the full commit SHA, with the human tag in
  a trailing comment: `uses: actions/checkout@<40-hex>  # v4.2.2`.
- Never pin to a branch or a moving tag. Local reusable workflows (`./.github/workflows/ci.yml`) are fine.
- Resolve and verify the SHA **through the GitHub API at pin time** (never copy a SHA from a blog, README or
  model memory):

```bash
# 1) tag -> object (lightweight tag gives a commit directly; annotated tag gives a tag object)
gh api repos/actions/checkout/git/ref/tags/v4.2.2 --jq '.object | {type, sha}'
# 2) if type == "tag", dereference to the commit
gh api repos/actions/checkout/git/tags/<sha-from-step-1> --jq '.object.sha'
# 3) confirm the commit exists in the repo and note its date/message
gh api repos/actions/checkout/commits/<commit-sha> --jq '{sha, date: .commit.committer.date, msg: .commit.message}'
```
- Prefer fewer actions: use `run:` steps with pinned CLIs where practical. For Trivy do **not** depend on a
  wrapper action; either install the CLI at a pinned version with checksum verification, or use the
  vendor's `setup-trivy` action pinned to a SHA with `version: v0.75.0`.
- Upgrades arrive as reviewed PRs (`.github/dependabot.yml`, ecosystem `github-actions`, weekly, devops
  owns the file). Dependabot understands SHA pins with version comments.
- Record each pin (action, tag, SHA, date verified, who) in the PR description.
- Second, offline-friendly verification (used in the 2026-10-10 audit, needs only git):
  `git ls-remote https://github.com/<owner>/<repo> "refs/tags/<tag>" "refs/tags/<tag>^{}"` and compare
  the SHA (for an annotated tag use the `^{}` line). `check` also covers composite actions in
  `.github/actions/*/action.yml`, not only workflows.

Current pins (all six verified against `git ls-remote` on 2026-10-10, every one is a lightweight tag whose
commit equals the pinned SHA): `actions/checkout` v7.0.1, `actions/upload-artifact` v7.0.1,
`actions/download-artifact` v8.0.1, `actions/cache` v6.1.0, `astral-sh/setup-uv` v10.2.0 (uv 0.12.24),
`docker/login-action` v4.6.0. Binary tools are not actions: `scripts/deploy/install_tool.sh` downloads
trivy 0.75.0, actionlint 1.7.12 and k6 2.3.0 from their GitHub releases and verifies a SHA-256 stored in the
repo (the six per-arch hashes were re-checked against the upstream checksum files on 2026-10-10 and match).

---------------------------------------------------------------------------------------------------

## 4. Workflow permissions policy

Top level of **every** workflow: `permissions: {contents: read}` (policy `actions.default_permissions`).
Elevations are declared per **job** and only for write permissions listed in
`actions.allowed_job_write_permissions`. `check` rejects: missing top-level block, a top-level value other
than the default, blanket `write-all`, any job write permission not allow-listed for that file, secrets read
by a job without `environment:`, and any non-`GITHUB_TOKEN` secret in a workflow listed in
`actions.secret_free_workflows`.

| Workflow | Trigger | Top level | Job-level permissions | Secrets read | Environment |
|---|---|---|---|---|---|
| `ci.yml` | `pull_request` (main), `push` (main), `workflow_dispatch`, `workflow_call` | `contents: read` | none | none (fork-safe, secret-free) | none |
| `release.yml` | `push` to `main` (deploy mandatory), `workflow_dispatch` (`require_deploy`, default true) | `contents: read` | job `ci` (reusable ci.yml): `contents: read`; `build-scan`: `contents: read`; **`publish`: `contents: read` + `packages: write` (with `pr-preview.yml` `publish`, the only `packages: write`)**; `deploy`: `contents: read` only (its `download-artifact` of `release-record` needs no `actions: read`, see below) | `GITHUB_TOKEN` (publish login); `RENDER_DEPLOY_HOOK_URL`, `RENDER_API_KEY`, `RENDER_SERVICE_ID` in `deploy` only | `deploy` -> `production` |
| `rollback.yml` | `workflow_dispatch` only, main only | `contents: read` | `verify-image`: `contents: read` + `packages: read`; `rollback`: `contents: read` | `GITHUB_TOKEN` (read login); hook and API credentials in `rollback` only | `rollback` -> `production` |
| `demo-unsafe-deploy.yml` | `workflow_dispatch` only, main only, typed confirmation | `contents: read` | `guard`: `contents: read` + `packages: read`; `deploy-unsafe`: `contents: read` | `GITHUB_TOKEN`; hook and API credentials in `deploy-unsafe` only | `deploy-unsafe` -> `demo-unsafe` (needs required reviewers) |
| `perf.yml` | `workflow_dispatch` only (typed `WRITE`) | `contents: read` | none | none (secret-free; uses variable `STOCKFLOW_BASE_URL`) | none |
| `demo-fixtures.yml` | weekly `schedule` (Mon 05:17 UTC) + `workflow_dispatch` | `contents: read` | none | none (secret-free) | none |
| `pr-preview.yml` | `pull_request` to `main` (`labeled`, `unlabeled`, `synchronize`, `reopened`, `closed`) | `contents: read` | `decide`: `contents: read`; `gate`: `contents: read` + `checks: read`; `build-scan`: `contents: read` (runs PR code, no secrets); **`publish`: `contents: read` + `packages: write`** (no secrets, no PR code, sha256-verified tarball, base-branch scripts); `preview`: `contents: read` + `pull-requests: read` (re-check that the PR is open, labelled, unchanged); `cleanup`: `contents: read`; **`report`: `contents: read` + `pull-requests: write`** (no secrets, no PR code, sanitised comment) | `GITHUB_TOKEN` (gate, publish, report); `RENDER_API_KEY`, `RENDER_SERVICE_ID` in `preview` and `cleanup` only | `preview`, `cleanup` -> `preview` |

Job names in release.yml: `ci` -> `build-scan` -> `publish` -> `deploy`. `release.yml`, `rollback.yml` and
`demo-unsafe-deploy.yml` share concurrency group `deploy-production` (`cancel-in-progress: false`), so a
demo deployment, a release and a rollback can never overlap.

Write permissions are allow-listed per file in the policy: `packages: write` for `release.yml` and `pr-preview.yml`, `pull-requests: write` for `pr-preview.yml` only (the sticky comment). Not allowed anywhere: `contents: write`, `id-token`, `actions: write`,
`security-events: write` (so no SARIF/code-scanning upload; scanner output is kept as workflow artifacts),
`pull_request_target`, `secrets: inherit`, `continue-on-error: true`.
A new write permission requires a policy change plus a justification in the PR.

---------------------------------------------------------------------------------------------------

## 5. Secrets and variables inventory

| Name | Kind | Scope | Used by | Notes |
|---|---|---|---|---|
| `RENDER_DEPLOY_HOOK_URL` | secret | **environment** `production` (and, separately, `demo-unsafe`); never repository-wide | `deploy` (release), `rollback`, `deploy-unsafe` via `scripts/deploy/deploy_render.py` | URL contains the hook key; passed as `env:` to one step; masked; rotate by regenerating the hook in Render |
| `RENDER_API_KEY` | secret, **optional** | same environments | same step; `deploy_lib.RenderApi` issues only GET requests to `https://api.render.com/v1` (list/get deploys) to report whether the deploy went `live` | Render API keys are **not scopable**: they carry the owner's full account rights. Create the key under a dedicated Render user/workspace with access to this one service if possible. Without it the pipeline still works and infers status from `/api/version` + smoke test |
| `RENDER_SERVICE_ID` | secret, **optional** | same environments | same step | identifier (`srv-...`), validated against `[A-Za-z0-9_-]{3,64}`; treated as a secret only to keep it out of logs |
| `RENDER_API_KEY`, `RENDER_SERVICE_ID` (preview) | secrets | **environment `preview`** only (it holds its OWN copies of the values: GitHub environment secrets cannot be read back or copied between environments) | `preview` and `cleanup` jobs of `pr-preview.yml` via `scripts/deploy/preview_tools.py` (create / find / redeploy / delete previews of the base service) | Here the key is **not** read-only: it creates and deletes services. Render keys are not scopable. **Observed 2026-10-12: the `preview` environment uses the same Render key value as `production`, i.e. an account-wide key that can reach the production service.** The recommended dedicated preview workspace and user (10.9 item 5) is **OPEN / not done**; this is an accepted risk (10.10) |
| `PREVIEW_PLAN` | variable, optional | environment `preview` | `preview` job | validated against `^[a-z0-9][a-z0-9_.-]{0,31}$`; unset = the base service's plan |
| `STOCKFLOW_BASE_URL` | variable | repository or environment | preflight, smoke, perf | public URL, not secret |
| `GITHUB_TOKEN` | automatic, per job | per job, see 4 | GHCR login: write in `publish` only; read in `verify-image`/`guard` | no PAT is used by any workflow |
| (none) | - | - | the application | no API keys, DB credentials or signing keys exist; `REPOSITORY_BACKEND=memory` |

Rules: do **not** define any of the three Render secrets at repository level, otherwise `demo-unsafe` and
`production` would share them and the environment isolation is lost. Not used, by decision: GHCR personal
access tokens in CI, registry credentials on Render (package is public), cloud provider keys, OIDC.
The presenter's own PAT/`gh auth` used to push the manual demo image lives only on their workstation
(scope `write:packages`, short expiry) and is outside the repository. Adding any new secret requires an entry
here. Local development uses an untracked `.env` (gitignored).

---------------------------------------------------------------------------------------------------

## 6. Scanner policy

| Gate | Tool and version here | Scope | Blocks when | Policy keys | Artifact (if any) |
|---|---|---|---|---|---|
| SAST | bandit 1.9.4 | `app/` | any finding with severity >= medium **and** confidence >= medium (a superset of high/high) | `bandit.*` | `reports/bandit.txt` |
| Dependency audit | pip-audit 2.10.1 on `uv export` of `uv.lock`, runtime + dev | all locked packages | **any** known vulnerability (no severity filter) unless a non-expired ignore exists | `pip_audit.*` | `reports/pip-audit.txt` |
| Filesystem scan | trivy 0.75.0 `fs` (vuln + secret) | repo tree minus `skip_dirs`, includes dev deps | HIGH/CRITICAL (including unfixed) or any secret | `trivy.fs.*`, `trivy.scanners_fs` | `reports/trivy-fs.txt` |
| Config scan | trivy `config` | `Dockerfile` (the only file Trivy's misconfiguration analyzers detected here: 1 config file; `compose.yaml` and workflows are not covered, workflows are covered by `check` + actionlint) | HIGH/CRITICAL misconfiguration (e.g. DS-0002 no `USER`) | `trivy.config.*` | `reports/trivy-config.txt` |
| Image scan | trivy `image` (vuln + secret), `linux/amd64` | the built image | HIGH/CRITICAL **with an available fix**, or any secret | `trivy.image.*` | `reports/trivy-image.txt` |
| Full image report | trivy `image`, no exit code | the built image | never (informational; includes unfixed) | `args trivy-image-report` | `reports/trivy-image-full.json` |
| Image guard | `docker run`/`docker export` grep | image filesystem | `demo/`, `tests/`, `DEMO-DEFECT`, `.github/` present | n/a (devops) | - |
| Policy self-check | `scripts/quality_policy.py check` | policy + mirrors + workflows | any problem | all | - |

Release integrity chain (`release.yml`): `ci` (all gates for the exact commit) -> `build-scan` builds
`linux/amd64`, runs the container, image guard and graceful-stop test, `docker save`s the image and Trivy-scans
**that tarball** -> `publish` (only job with `packages: write`) `docker load`s the same tarball and pushes
`sha-<git sha>` and `<version>` tags, captures the digest the registry reports (both pushes must agree), and
verifies the registry's OCI labels name this commit -> `deploy` sends only `ghcr.io/<owner>/stockflow@sha256:...`
to Render, waits for Render to report that deploy `live` when the API credentials exist, runs the smoke test
and `release_tools.py verify-deployment`. So the scanned bytes are the published bytes; nothing is rebuilt
between scan and push, and `publish` checks the tarball's sha256 against the value `build-scan` computed.

Deploy semantics (`release.yml` `deploy` job, environment `production`): deployment is **mandatory on push to
`main`**. `release_tools.py preflight` fails the job (after the image has already been published) when
`RENDER_DEPLOY_HOOK_URL` or `STOCKFLOW_BASE_URL` is missing; only a manual run with `require_deploy: false` may
skip it, and then the record says `deployed=false`. The job then (1) downloads `release-record` (artifact of
this run only), (2) triggers the hook through `deploy_render.py` with the image taken from the `publish` job
output, optionally polling the Render API with `RENDER_API_KEY` + `RENDER_SERVICE_ID` (GET only) until that
deploy is `live` or failed, (3) smoke-tests the target for this commit, and (4) `verify-deployment` fails
unless the release record's digest equals the digest actually sent to Render (`deploy-result.json`), the record
names this commit and version, `render_status` is `live` or unpolled, and the serving `/api/version` reports
this git sha, version and name `stockflow`. A green run therefore means: scanned, published, deployed and
verified.

Artifact trust: `actions/download-artifact` is called with `name: release-record` and neither `run-id` nor
`github-token`, so it can only read artifacts of the current workflow run; it needs no `actions: read`
permission (that is required only for cross-run downloads) and `contents: read` is enough. The artifact is
never the source of what gets deployed; a tampered record can only make verification fail.

Workflow lint: `actionlint` 1.7.12 (`make workflow-lint`, installed by `install_tool.sh` with a pinned SHA-256).
Tool config: `config/bandit.yaml` (no global skips), `config/trivy.yaml` (operational options only;
gate semantics are rejected there by `check`), `.trivyignore` (7).

---------------------------------------------------------------------------------------------------

## 7. Exception process

An exception is the *last* resort after trying to fix, upgrade or replace. Each one needs **id, reason,
expiry**, a PR reviewed by a second person, and removal as soon as the root cause is fixed. Expired
entries fail `check` (CI) and are not honoured by the generated tool flags. Maximum window: 90 days
(`pip_audit.ignore_max_days`, `trivy.ignore_max_days`).

| Tool | Where | Format |
|---|---|---|
| pip-audit | `pip_audit.ignore` in `config/quality-policy.yaml` | `- {id: GHSA-xxxx-xxxx-xxxx, reason: "why not exploitable / mitigation", expires: 2026-12-01}`; emitted as `--ignore-vuln <id>` by `args pip-audit` |
| Trivy | `.trivyignore` | a block: `# justification: <reason>` line, then `CVE-YYYY-NNNN exp:YYYY-MM-DD` (Trivy-native expiry) |
| Bandit | inline at the offending line | `# nosec B### - <reason>`; global `skips` are forbidden. Existing: `app/config.py` `host = "0.0.0.0"` is B104, accepted because the container must bind all interfaces (Render injects `PORT`, traffic only arrives via Render's proxy) |
| Actions pin | none | there is no exception to SHA pinning |
| Permissions | policy `allowed_job_write_permissions` | needs a policy edit and reviewer sign-off |

### 7.1 Not exceptions (do not do these)
Lowering `coverage.fail_under`, adding `|| true`, `continue-on-error`, `--exit-code 0`, `--exit-zero`,
excluding paths from the bandit run, adding `# nosec` without an id and a reason.

### 7.2 Why image scans use `ignore_unfixed: true` (the one relaxation)
Verified on 2026-10-10 against the current `python:3.12-slim` (Debian 13.7, linux/amd64):

| Mode | Result |
|---|---|
| `trivy image --severity HIGH,CRITICAL` (strict) | 44 HIGH findings, **all** with status `affected`/`fix_deferred` and no fixed version (8 distinct CVEs: util-linux family x9 packages, ncurses, systemd, acl, perl-base) -> exit 1 |
| same with `--ignore-unfixed` | 0 findings -> exit 0 |
| `python:3.12.0-slim` (stale tag) with `--ignore-unfixed` | 89 findings (76 HIGH, 13 CRITICAL) with fixes available -> exit 1 |

Strict mode would make the *clean baseline* permanently red for reasons nobody in this repo can act on, which
trains people to bypass gates. With `ignore_unfixed: true`: (a) anything that has a fix still blocks, so a stale
base tag or image rebuild lag fails hard (see the third row); (b) the **complete** list including unfixed
findings is produced on every run by the non-blocking `trivy-image-report` step and uploaded as an artifact,
so nothing is hidden; (c) the filesystem scan of our own Python dependencies stays strict
(`trivy.fs.ignore_unfixed: false`), and pip-audit fails on any known vulnerability. Alternatives considered:
a `.trivyignore` entry per CVE (churns daily, 8+ entries, each needing expiry) and a stricter base such as
`python:3.12-alpine`, which had 0 HIGH/CRITICAL on the same date but changes the architect's runtime base
(musl vs glibc). The architect may switch the base and flip `trivy.image.ignore_unfixed` back to `false`
with no other change.

---------------------------------------------------------------------------------------------------

## 8. Known limitations

- No authentication or authorization on the API or dashboard: the app is a public demo with seeded,
  non-persistent data. Anyone can create products/orders (resets on restart). No rate limiting.
- No TLS termination in the app; Render terminates TLS. No HSTS/CSP/security headers are set by the app
  unless the backend adds them (recommended: `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  no-referrer`, a strict `Content-Security-Policy` since the UI has no CDN assets).
- **No SBOM and no provenance/attestation, and images are not signed** (no cosign, no
  `actions/attest-build-provenance`, no `docker buildx --sbom/--provenance`). Consumers cannot verify that an
  image was built by this workflow; the only identity is the digest plus the OCI labels. Adding attestations
  needs `id-token: write` and `attestations: write`, which the policy currently forbids.
- **The version tag (`ghcr.io/<owner>/stockflow:<version>`) is mutable**: every push to `main` with an
  unchanged `pyproject.toml` version re-points it. Only `sha-<git sha>` tags and, above all, the digest are
  identifiers. Deploys and rollbacks always use `@sha256:<digest>` (tags are refused by `deploy_render.py`).
  Render's service default image (`render.yaml`) is a tag but is only used by manual deploys.
- `uv export --no-hashes` is used because pip-audit `--require-hashes` needs hash-pinned requirements; the
  lockfile itself is hash-verified by `uv sync --frozen`, but the audited requirements text is not.
- pip-audit/Trivy only know *published* advisories; the advisory DB is fetched at run time, so results change
  over time even without code changes (a green run can turn red tomorrow; that is intended). The weekly
  `demo-fixtures.yml` run exists for the opposite reason: to notice when a demo advisory is withdrawn.
- Bandit is a pattern-based SAST: it does not find logic flaws. Hard-coded passwords (B105/B106) are LOW
  severity and are therefore **not** blocking at the medium threshold; the demo `bad_security` scenario uses
  `subprocess(..., shell=True)` (B602, High/High) which is.
- Unfixed OS-package CVEs in the base image are reported but not blocking (7.2).
- The Debian base ships 11 setuid binaries (`mount`, `umount`, `su`, `passwd`, ...). The Dockerfile strips all
  setuid/setgid bits in the runtime stage and `image_guard.sh` fails the build if any remain; the unfixed
  util-linux CVEs concern `mount` helpers, so this narrows their practical reach but they stay reported.
- The deploy hook has no scoping or IP allow-list; confidentiality of the URL is the only control.
  `RENDER_API_KEY` is account-scoped at Render (see 5); only GET requests are sent by our code.
- Deployment records are plain workflow artifacts (90 days), not signed or tamper-evident.
- The image tarball is handed from `build-scan` to `publish` as a workflow artifact; since 2026-10-11 its
  sha256 is verified between jobs. The `release-record` artifact is trusted only within the current run.
- Fork PRs still execute fork-controlled code (tests, `uv sync`, the fork's Dockerfile) on a GitHub-hosted
  runner. That is safe only because the job has no secrets and a read-only token; it is the reason
  `pull_request_target` is banned and ci.yml must stay secret-free.
- Controlled-failure demo: the broken image is pushed to GHCR **manually by the presenter**; nothing in CI
  can publish a defective build. That manual path is an accepted risk (T13): it uses a personal token on
  a workstation, and unless `demo-unsafe` has its own hook and service the production service is the target.
- Secret scanning covers the working tree only. The repository has no commits yet, so there is no history to
  scan; once it has, run a history-aware scanner once (for example `gitleaks detect`) before making it public.
- Render image pull from a public GHCR package is unauthenticated; GHCR rate limits can in theory delay a
  deploy.
- **PR previews trust the people who can push branches here.** Because `pr-preview.yml` is a `pull_request`
  workflow, its definition comes from the PR merge commit; a writer can change it to read the `preview`
  environment's secrets. Environment rules (10.9) and a dedicated Render workspace would limit the damage; **as
  configured on 2026-10-12 neither is in place** (10.10), so a writer can obtain the account-wide Render key,
  which is the same value as production's. Fork and Dependabot PRs are excluded and cannot reach those secrets.
- The `quality-gate` check used to authorise a preview is spoofable by any workflow on the same commit, and the
  Trivy/test steps of a PR image run PR-controlled commands: for previews they are advisory, not a trust anchor.
- Previews: GHCR `pr-<n>` tags are never deleted. Observed on 2026-10-12 (PR #3, runs 38209130008 and
  38209469656): create/deploy and cleanup work, previews run on the free plan, the `repo@sha256` form of `imagePath`
  is accepted, and the base env vars are inherited (`DEPLOY_ENVIRONMENT=production` is reported by the preview).
  NOT VERIFIED: redeploy on a later push, a paid `PREVIEW_PLAN`, and the cleanup/re-check race branches (T17: fixed in code and
  unit-tested only).
- Single worker, in-memory data: a restart loses state by design.
- Dev VM quirk (aarch64 + containerd image store): `trivy image` with the default Docker source fails on a
  foreign-platform amd64 image (`content digest ... not found`). Use the tarball route in 9 (the Makefile
  already does: `docker-save`, `trivy-image-tar`).

---------------------------------------------------------------------------------------------------

## 9. Verified commands

All run from the repo root with `export PATH=$HOME/.local/bin:$PATH`; flags come from the policy via
`python scripts/quality_policy.py args <tool>` (shown expanded below for reading, produced 2026-10-10).
Run results are in the security agent report; re-verify with `make security` once devops wires it.

```bash
# policy self-check (also verifies pyproject mirror, workflows, ignores, drift)
uv run python scripts/quality_policy.py check
uv run python scripts/quality_policy.py get coverage.fail_under          # 85

# bandit (SAST). Exit 1 on any >=medium/medium finding.
uv run bandit $(uv run python scripts/quality_policy.py args bandit) | tee reports/bandit.txt
#  == bandit -r app -c config/bandit.yaml --severity-level medium --confidence-level medium

# pip-audit via uv export (two steps; the file is a gitignored build product)
mkdir -p reports
uv export $(uv run python scripts/quality_policy.py args uv-export)
#  == uv export --quiet --frozen --no-hashes --no-emit-project --all-groups -o reports/requirements-audit.txt
uv run pip-audit $(uv run python scripts/quality_policy.py args pip-audit) | tee reports/pip-audit.txt
#  == pip-audit -r reports/requirements-audit.txt --no-deps --disable-pip --strict --progress-spinner off [--ignore-vuln ID ...]

# trivy filesystem (vuln + secret, dev deps included)
trivy fs $(uv run python scripts/quality_policy.py args trivy-fs) . | tee reports/trivy-fs.txt
#  == trivy fs --config config/trivy.yaml --ignorefile .trivyignore --no-progress --skip-dirs <dirs>
#       --scanners vuln,secret --severity HIGH,CRITICAL --exit-code 1 --include-dev-deps .

# trivy config (Dockerfile / IaC); takes a directory
trivy config $(uv run python scripts/quality_policy.py args trivy-config) . | tee reports/trivy-config.txt
#  == trivy config --config config/trivy.yaml --ignorefile .trivyignore --skip-dirs <dirs> --severity HIGH,CRITICAL --exit-code 1 .

# trivy image (blocking) - preferred portable route: save the amd64 image to a tarball
docker save --platform linux/amd64 "$IMAGE" -o reports/image.tar          # sudo on the dev VM
trivy image $(uv run python scripts/quality_policy.py args trivy-image) --input reports/image.tar | tee reports/trivy-image.txt
#  == trivy image --config config/trivy.yaml --ignorefile .trivyignore --no-progress --scanners vuln,secret
#       --severity HIGH,CRITICAL --platform linux/amd64 --exit-code 1 --ignore-unfixed --input reports/image.tar
# (on a native amd64 CI runner "trivy image <flags> $IMAGE" with the default docker source also works)

# trivy image full report (non-blocking, includes unfixed findings)
trivy image $(uv run python scripts/quality_policy.py args trivy-image-report) --input reports/image.tar \
  --format json --output reports/trivy-image-full.json

# k6 thresholds are passed as env/flags, so load-tests never hard-code them
k6 run $(uv run python scripts/quality_policy.py args k6) load-tests/<script>.js
#  == -e P95_MS=500 -e ERROR_RATE_MAX=0.01 --vus 10 --duration 30s
```

First run downloads the Trivy vulnerability DB (about 121 MiB from `mirror.gcr.io/aquasec/trivy-db:2`, 29 s
on this VM; later runs reuse `~/.cache/trivy`). In CI cache `~/.cache/trivy` keyed by date and keep
`db.skip-java-db-update: true` (already in `config/trivy.yaml`).

The Makefile targets `security-bandit`, `security-audit`, `security-trivy-fs`, `security-trivy-config`,
`docker-save`, `trivy-image-tar` and `perf` wrap exactly these commands (`make security` runs the first four plus
`policy-check`). In CI the tools are installed by `scripts/deploy/install_tool.sh` / the `setup-trivy` composite
action, not by marketplace wrappers.

`| tee` requires `set -o pipefail` (GitHub Actions' default `bash -e -o pipefail` has it; the Makefile must set
`SHELL := bash` and `.SHELLFLAGS := -eu -o pipefail -c`), otherwise a failing scanner would be masked.

---------------------------------------------------------------------------------------------------

## 10. DevOps hand-off and audit status

Devops owns `.github/`, `Makefile`, `Dockerfile`, `scripts/deploy/`. The requirements below were written
before the pipeline existed; section 10.8 records how the delivered files measure up (audit of 2026-10-10).
`python scripts/quality_policy.py check` parses every workflow and composite action and must pass in CI
(job `config-validation` runs it through `make policy-check`).

### 10.1 Policy wiring
1. Never type a threshold. Use `get`/`args` (section 9). The drift scan fails on literal `--severity`,
   `--exit-code`, `--ignore-unfixed`, `--ignore-vuln`, `--severity-level`, `--confidence-level`,
   `--exit-zero`, `--cov-fail-under`/`--fail-under N`, and `<tool> ... || true`.
2. Makefile: `SHELL := bash`, `.SHELLFLAGS := -eu -o pipefail -c` because gates are piped into `tee`.
3. `quality-gate` must `needs:` every job and require each to be exactly `success`.

### 10.2 Permissions (section 4)
Every workflow starts with `permissions: contents: read`. Job-level write only as allow-listed (today:
`packages: write` on release.yml `publish`). No `pull_request_target`, `secrets: inherit`,
`continue-on-error: true`. Reusable ci.yml is called without a `secrets:` block.

### 10.3 SHA pinning and tool installs
Pin every `uses:` (workflows **and** composite actions) to a 40-hex SHA with a tag comment, verified through
`gh api` or `git ls-remote` (section 3). Install binaries only through `scripts/deploy/install_tool.sh`
(pinned version + SHA-256). Keep Dockerfile bases pinned by tag and digest. Dependabot covers actions
(`/` and `/.github/actions/*`), uv and docker.

### 10.4 Secrets scoping
Secrets only as `env:` of the one step that needs them, in a job with `environment:`; presence flags
(`secrets.X != ''`) may be computed in job `env:`. `RENDER_*` secrets are never defined at repository level.
Restrict `production` to the `main` branch; configure **required reviewers on `demo-unsafe`**. Build args are
limited to `APP_VERSION`, `GIT_SHA`, `BUILD_TIME`.

### 10.5 Fork-PR rules
ci.yml: `pull_request` only, no secret references, no GHCR login, no push. No attacker-controlled text in
`run:` (`${{ ... }}` of `github.head_ref`, PR/issue text, commit messages, `inputs.*` goes through `env:`);
`check` fails on the direct form. Repo settings to document: Actions "require approval for all outside
collaborators", default workflow permissions "read repository contents", Dependabot alerts, branch
protection requiring only `quality-gate`.

### 10.6 Scanner outputs to upload (artifacts, `if: always()`, SHA-pinned upload-artifact)
`reports/coverage.xml`, `bandit.txt`, `pip-audit.txt`, `trivy-fs.txt`, `trivy-config.txt`,
`trivy-image.txt`, `trivy-image-full.json`, `deployment-record.json`, `release-record.json`. No SARIF.

### 10.7 Image guards and Dockerfile hardening
Image guard (`scripts/deploy/image_guard.sh`, run by `docker_test.sh` in CI and release): non-root, no
`demo/ tests/ load-tests/ docs/ .github/ .demo-state/ scripts/ reports/`, no `DEMO-DEFECT`, no git/env/db
files, no uv/pip/gcc/make/git, app code not writable. Also enforced: no setuid/setgid files
(`find / -xdev -perm /6000 -type f` empty); the Dockerfile strips the bits with
`find / -xdev -perm /6000 -type f -exec chmod a-s {} +`.

### 10.8 Audit results (2026-10-10), real outcomes
Passed:
- `uv run python scripts/quality_policy.py check`: OK (workflows, composite actions, drift scan included).
- `make security`: policy-check, bandit (no issues, 1378 lines), pip-audit (no known vulnerabilities), trivy fs
  (uv.lock 0 vulnerabilities, 0 secrets), trivy config (Dockerfile, 0 misconfigurations): exit 0.
- `trivy fs --scanners secret` over the whole tree except `.venv` and `.git`: 0 secrets; a regex grep for
  AWS/GitHub/Slack/Render key shapes and private-key headers found nothing; no `.env` file exists.
- actionlint 1.7.12 over all workflows: clean. `bash -n` on all `scripts/deploy/*.sh`: clean.
- All 6 pinned action SHAs equal the tag's commit per `git ls-remote`; no unpinned non-local `uses:`.
- Expression injection: no `${{ }}` appears inside any `run:` block of any workflow or composite action;
  all dynamic values (`inputs.*`, step outputs, `github.sha`) travel through `env:`.
- Permissions: top-level `contents: read` everywhere; the only write is `packages: write` on `publish`.
  Secrets are read only in jobs with `environment:`; ci.yml, perf.yml, demo-fixtures.yml are secret-free.
- Fork-PR exposure: ci.yml is `pull_request`/`push`/`workflow_dispatch`/`workflow_call`, no secrets, no
  `pull_request_target`; `persist-credentials: false` on every checkout.
- Upstream SHA-256 values in `install_tool.sh` match the published checksum files (6 of 6).
- Dockerfile: bases pinned by tag+digest, multi-stage, non-root uid 10001, no pip/uv in runtime, allow-list
  `.dockerignore` (`*`, `!app`, `!pyproject.toml`, `!uv.lock`), exec-form CMD, HEALTHCHECK, no secrets in
  ARG/ENV, `--read-only --cap-drop ALL --no-new-privileges` in `docker_test.sh` and compose.

Re-audit of 2026-10-11 after the `release.yml` / `deploy_lib.py` / `release_tools.py` changes:
- `uv run python scripts/quality_policy.py check`: OK. `make security`: exit 0 (bandit no issues in 1379 lines,
  pip-audit clean, trivy fs 0 vulnerabilities/0 secrets, trivy config 0 misconfigurations). actionlint clean.
- Expression injection: still no `${{ }}` inside any `run:` block. New values (`DEPLOY_REQUIRED`,
  `EXPECTED_TAR_SHA256`, step outcomes, `image_sha256`) arrive through `env:`; `EXPECTED_TAR_SHA256` is
  regex-checked (`^[0-9a-f]{64}$`) before use. `DEPLOY_REQUIRED` is a boolean expression of
  `github.event_name` and a boolean input.
- Permissions: unchanged; `deploy` still holds `contents: read` only, and `check` confirms the three Render
  secrets are read only in a job with `environment:`.
- Secret exposure: `RENDER_DEPLOY_HOOK_URL`, `RENDER_API_KEY`, `RENDER_SERVICE_ID` are passed as `env:` of the
  single `deploy_render.py` step only (GitHub also masks all `secrets.*` values). The hook and each derived
  value (URL with `imgURL`, query values) are additionally `::add-mask::`ed. Hook failures raise `DeployError`
  built from the HTTP code or from a `redact()`ed reason, `from None`, so the URL never reaches a traceback;
  Render API failures contain the endpoint path and status code, or the `redact()`ed reason (key removed). The
  API key is sent only to the constant `https://api.render.com/v1`. The new `verify-deployment` and `record`
  code print digests, shas, versions and `/api/version` fields only.
- Artifact trust: as described in 6 and T14, the deploy job reads only this run's `release-record` and treats it
  as a cross-check.
- Previously reported findings now fixed by DevOps: `trivy-image-tar` writes the full report first
  (`Makefile:151`), setuid/setgid bits are stripped (`Dockerfile:39`) and enforced by `image_guard.sh:22-35`, the
  tarball sha256 is verified between `build-scan` and `publish`, and `md_safe` (`deploy_lib.py:549`) sanitises
  free text written to step summaries.

Open findings for devops (not in security's ownership):
1. **LOW, `scripts/deploy/deploy_lib.py:138-143` (`default_opener`) and `:230-236` (`RenderApi._get`)**:
   `urllib` follows redirects and re-sends custom headers, including `Authorization: Bearer <RENDER_API_KEY>`,
   to the redirect target (possibly another host). Use an opener built with a `HTTPRedirectHandler` subclass
   that refuses redirects (raise `DeployError`) for authenticated GETs, or drop `Authorization` when the host
   changes. The same opener also POSTs the hook URL; refuse redirects there as well.
2. **LOW, `scripts/deploy/deploy_lib.py:381`**: the error text says `STOCKFLOW_BASE_URL must start with
   https://` but the check accepts `http://` as well (as does `fetch_version`, line 338). For a mandatory
   production verification require `https://` (allow `http://localhost` only via an explicit flag).
3. **INFO**: `RENDER_API_KEY` cannot be scoped at Render; keep it optional, create it under a dedicated user,
   and rotate it after demos. `demo-unsafe` must have its own secrets or required reviewers, otherwise the
   typed confirmation is the only barrier before production receives a broken image.
4. **INFO**: the same commit on `main` runs ci.yml twice (own `push` trigger and the `ci` job of release.yml).
   Harmless (different concurrency groups) but doubles runner minutes.
5. **INFO**: repository settings that no file can enforce and that must be configured in GitHub: `production`
   environment restricted to `main`; `demo-unsafe` with required reviewers; Actions approval for outside
   collaborators; read-only default `GITHUB_TOKEN`; branch protection requiring `quality-gate`.
6. **INFO**: because deployment is now mandatory on push, the first push to `main` before the Render service,
   `RENDER_DEPLOY_HOOK_URL` and `STOCKFLOW_BASE_URL` exist ends red by design (after publishing the image);
   `docs/SETUP_GITHUB_RENDER.md` must say so.

### 10.9 PR preview review (2026-10-11): `pr-preview.yml`, `preview_lib.py`, `preview_tools.py`

Verdict: acceptable **for a repository where everyone with write access is trusted**, with the environment
settings below. No path exists for a fork, Dependabot or non-collaborator to reach the Render credentials, and
no PR code runs in a job that holds a secret or `packages: write`/`pull-requests: write`.

Checked and sound: no `${{ }}` inside any `run:` (event data arrives as `env:`, `with: ref:` values are not shell);
job-level guard plus script-level `decide` (deny by default, `closed`/`unlabeled`/`labeled`/`synchronize`/`reopened`
table); `build-scan` (PR code) has no secrets and no write permission; `publish` verifies the tarball sha256 and
runs base scripts; `preview`/`cleanup` run base scripts, validate the image ref as a digest of this owner, and
refuse to delete the base service; Render and GitHub clients are https-only with redirects refused; the preview
URL must be `https://<name>.onrender.com` before it is smoke-tested or shown; every comment value passes `md_safe`.

Exact `preview` environment settings (GitHub -> Settings -> Environments -> `preview`):
1. **Required reviewers**: at least one maintainer who is not the usual PR author; enable **Prevent self-review**.
   A malicious workflow edit still reaches the secret only after a reviewer approved the run, so approvers must
   read the diff of `.github/` and `scripts/deploy/` first. Trade-off: every deploy and cleanup waits for approval.
   For a one-person demo repository this may be omitted, accepting T15 (**this is the current state**, see 10.10).
2. **Deployment branches and tags -> Selected**: add the pattern `refs/pull/*/merge` (PR runs execute on that ref; the
   behaviour of this rule for `pull_request` events is community-documented and NOT VERIFIED by us: no such rule is set). It stops any
   push-, schedule- or dispatch-triggered workflow on another branch from using the environment. It does **not**
   restrict which collaborator opens the PR, so it is not a substitute for item 1.
3. **Disable "Allow administrators to bypass"**.
4. **Secrets**: `RENDER_API_KEY` and `RENDER_SERVICE_ID` only here, never repository- or organization-wide, never the
   production values.
5. **Blast radius (most important; OPEN, not done)**: create a separate Render workspace for previews with its own base image-backed
   service (the same GHCR image path), create the API key from a dedicated Render user that is a member of that
   workspace only, and put that service's id in the secret. An exfiltrated key then cannot touch production.
6. Repository: CODEOWNERS for `.github/**`, `scripts/deploy/**`, `Dockerfile`, `config/**`; label `preview` only after
   reading those diffs; keep the base service free of secret env vars (T19).

Findings for DevOps (file:line):
1. **MEDIUM, cleanup race: FIXED in code (commit e0f42d8), unit-tested, real-event behaviour NOT VERIFIED.** The original
   problem: a pending or running cleanup (`unlabeled`, `closed`) could be cancelled by a later update event, and `closed` only
   cleaned up when the label was still set, so `labeled` -> `unlabeled` -> `synchronize` -> `closed` orphaned a live preview.
   Fix as implemented (re-read 2026-10-12): `preview_lib.decide` now maps `closed` to `cleanup` unconditionally;
   `.github/workflows/pr-preview.yml:36-37` puts `closed` / `unlabeled` runs into the group `pr-preview-<n>-cleanup`
   with `cancel-in-progress: false`, while updates keep the group `pr-preview-<n>` and cancel each other; the `preview`
   job (`pull-requests: read`) runs `preview_tools.py recheck --phase before` (skips the deploy when the PR is closed,
   unlabelled or at another head) and `--phase after` (deletes the just-created preview when the PR was closed or
   unlabelled meanwhile; a changed head only supersedes the run and keeps the preview for the newer run), using
   the pull request returned by `GET /repos/<repo>/pulls/<n>` (`preview_lib.pr_state`); the smoke test only runs when the second
   re-check says the state is still valid. Covered by unit tests; on real GitHub events only the plain `closed` path with
   the label set was exercised (cleanup run 38209469656). Not exercised: unlabel then push, closed after unlabel,
   supersede during deploy. New cost: every closed same-repo, non-Dependabot PR starts a `cleanup` job in environment
   `preview` (one Render API call with the account-wide key), even if it never had a preview.
2. **LOW, comment hijack: FIXED in code (commit e0f42d8), unit-tested.** `preview_lib.py:352` now requires the comment
   author to be `type == "Bot"` **and** `login == "github-actions[bot]"` (`BOT_LOGIN`) as well as the marker, so another
   GitHub App's comment cannot be edited by the `report` job.
3. **INFO, `scripts/deploy/preview_lib.py` `gate_state`.** Requiring the Actions app is good, but the check name is
   spoofable by any workflow on the commit (T16); document it as advisory. A stronger variant is to query
   `/actions/runs?head_sha=<sha>&event=pull_request` and require `ci.yml` to be `success`, which still comes from the
   PR's own workflow.
4. **INFO, `.github/workflows/pr-preview.yml` `publish`.** Pushes into the production package; document that tags are
   never trusted. A scheduled job to delete old `pr-<n>*` versions needs `packages: write` plus delete rights and is not
   part of this design.

### 10.10 GitHub and Render configuration as observed on 2026-10-12, and accepted risks

Observed (reported by the repository owner and by workflow runs; not independently re-read from the GitHub API):
- PR preview end to end: run **38209130008** (same-repo PR #3 with label `preview`) deployed a preview, run
  **38209469656** deleted it. The preview ran on the Render free plan, accepted the digest form of `imagePath`, and
  inherited the base service's env vars (it reported `DEPLOY_ENVIRONMENT=production`).
- Environment `production`: restricted to `main`.
- Environment `demo-unsafe`: required reviewer (the repository owner).
- Environment `preview`: **no deployment-branch rule and no required reviewers.**
- Branch protection: requires only `quality-gate`; **administrators can bypass it.**
- GitHub environment secrets cannot be read back or copied between environments, so `preview` holds its own
  `RENDER_API_KEY` / `RENDER_SERVICE_ID`. The owner entered the **same Render API key value as production**.

Accepted risks (solo repository, demo purpose). Each is explicit and remains open until the owner changes the setting:
1. **Preview key = production key (account-wide).** Anyone who can run a modified `pr-preview.yml` (anyone with write
   access, T15) can read it and act on every Render service of that account, production included. Mitigation not
   applied: dedicated preview workspace + dedicated Render user (10.9 item 5). Interim hygiene: the only writer is the
   owner; rotate the key after demos or if a collaborator is ever added; do not add collaborators before 10.9
   items 1, 2 and 5 are done.
2. **`preview` has no reviewers and no deployment-branch rule**, so a PR run reaches the secrets without approval, and
   a push-triggered workflow on any branch could request `environment: preview` as well.
3. **Branch protection can be bypassed by administrators** and requires only `quality-gate` (no review requirement),
   so the owner can merge or push changes to `.github/` without a second pair of eyes.
4. Items already listed in 8 stay as they are: the gate is advisory (T16) and `pr-<n>` tags accumulate (T18). The cleanup
   race (T17) is fixed in code and unit-tested; its race branches are NOT VERIFIED on real GitHub events.

NOT VERIFIED (not exercised yet): required-reviewer and deployment-branch behaviour for `pull_request` runs, a
second collaborator's PR, `PREVIEW_PLAN` on a paid plan, a preview redeploy after a new push, a failed preview
deploy path against the real Render API, and `demo-unsafe-deploy.yml` end to end against a separate demo service.

---------------------------------------------------------------------------------------------------

## 11. Requirements for backend and demo-engineer

- Backend: keep `# nosec B104` on the bind-all default but add the reason, e.g. `# nosec B104 - container bind,
  traffic only via Render proxy`. Do not read request headers to choose behaviour. If you add request-id
  middleware validate with `^[A-Za-z0-9._-]{1,64}$` or generate a UUID. Prefer sending the baseline security
  headers listed in section 8. Never log raw header values or request bodies.
- Demo-engineer (state as delivered in `demo/README.md`): `bad_security` trips bandit with B602
  (High/High); `bad_dependency` pins `python-multipart==0.0.6` so pip-audit and `trivy fs` both fail;
  `bad_container` rewrites the base `FROM` to a 2023 Debian image (92 fixable HIGH/CRITICAL), which blocks
  even with `ignore_unfixed: true` because the findings have fixes; fixtures never use the names
  `requirements.txt`, `uv.lock`, `poetry.lock` (Trivy's analyzers would flag the clean tree) and never contain
  secret-shaped strings; no scenario touches `config/`, `.trivyignore`, workflows, Makefile or `[tool.*]`.
- Runtime faults (`slow_api`, `unhealthy_health`, `bad_deployment`) need BOTH `STOCKFLOW_DEMO_FAULT=<id>` and
  `STOCKFLOW_DEMO_ALLOW_RUNTIME_FAILURES=true`, read from the process environment only, never from request
  data, and the engine itself refuses to inject them unless explicitly allowed.
- Presentation convention: defect scenarios live on branches named `demo/<scenario>` and are never merged to
  `main` (baseline test + image guard). The broken image for the controlled failed deployment is built and
  pushed to GHCR **by the presenter**; `release.yml` never publishes a defective build. Afterwards delete the
  demo package version and, if `demo-unsafe` shared production credentials, run `rollback.yml`.
- Deploy workflow recap for the demo: `demo-unsafe-deploy.yml` (typed `DEPLOY-BROKEN-IMAGE`, environment
  `demo-unsafe`) -> expected red smoke test -> `rollback.yml` with the previous good digest.

---------------------------------------------------------------------------------------------------

## 12. Design note: `tests/test_quality_policy.py` (QA)

`scripts/` is not a package. Load the module with `importlib.util.spec_from_file_location` (or add `scripts`
to `sys.path` in a fixture). Everything is pure and takes `root`/`today` explicitly; no network.
Use `tmp_path` to build a miniature repo (copy `config/quality-policy.yaml`, `config/bandit.yaml`,
`config/trivy.yaml`, `.trivyignore`, `pyproject.toml`, `.python-version` or write minimal ones) and pass
`today=dt.date(2026, 10, 10)`.

| Function | Cases to cover |
|---|---|
| `load_yaml` / `load_policy` | valid file; duplicate key -> `PolicyError` naming both lines; invalid YAML; non-mapping top level; unreadable path |
| `get_value`, `format_value` | scalar, bool -> `true`/`false`, list -> comma-joined, dict -> JSON, list index `a.0`, unknown key -> `PolicyError` |
| `validate_schema` | real policy has zero problems; missing key; unknown key; wrong type (`fail_under: "85"`); bad enum (`severity: critical`) |
| `check_floors` | `coverage.fail_under` 84 fails, 85 and 90 pass; `trivy.fs.ignore_unfixed: true` fails; dropping CRITICAL from severities fails; `exit_code: 0` fails; `actions.default_permissions` widening fails |
| `check_ignore_entries` | valid; missing reason/expires; duplicate id; short reason; expired (`expires` < today) fails; `expires == today` passes; beyond `max_days` fails; non-date |
| `active_pip_audit_ignores` / `build_args(..., "pip-audit")` | expired entry omitted from `--ignore-vuln`; active included |
| `check_trivyignore` | empty/comment-only file passes; entry without justification, without `exp:`, expired, malformed, duplicate |
| `check_mirrors` | matches in the real repo; `fail_under` mismatch; branch/source/mypy mismatch; `.python-version` mismatch; `skips` in bandit.yaml; gate key in trivy.yaml |
| `check_pins`, `check_workflows` | pins in workflows **and** `.github/actions/*/action.yml` (an unpinned composite-action step fails); local `./` refs allowed |
| `check_secret_scoping` | `secrets.X` in a job without `environment:` fails; `GITHUB_TOKEN` exempt; any secret in a `secret_free_workflows` file fails; secret in top-level `env` fails |
| `check_workflow` | passing minimal workflow; `@v4` unpinned; 40-hex pinned OK; `./` local OK; missing/widened top permissions; job `packages: write` OK only in `release.yml`; `contents: write` rejected; `pull_request_target`; `secrets: inherit`; `continue-on-error: true`; `${{ github.event.pull_request.title }}` and `${{ inputs.reason }}` inside `run:` rejected but allowed in `env:` |
| `scan_text_for_drift` | literal `--severity HIGH`, `--exit-code 0`, `--ignore-unfixed`, `--cov-fail-under=85`, `bandit ... || true` flagged; `--severity $(SEV)`/`--severity "${{ ... }}"` and comment lines not flagged |
| `build_args` | exact expected strings for each tool (snapshot the 8 outputs of section 9); `bandit` honours `fail_on_findings: false` via `--exit-zero`; `trivy-image-report` has neither `--exit-code` nor `--ignore-unfixed`; unknown tool -> `PolicyError` |
| `run_check` | the real repository returns `[]` (this is the guard that `pyproject.toml` and the policy agree); each mutation above returns at least one problem; `main(["check"])` exit codes 0/1/2 via `capsys` |

Also assert in a test that `pyproject.toml` `[tool.coverage.report] fail_under` equals
`get coverage.fail_under` (this is a second, independent statement of the mirror rule from architecture 12).
