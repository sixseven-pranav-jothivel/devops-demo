# Setup: GitHub repository and Render service

This is the checklist for the first real run of the pipeline. Honest status: **none of the GitHub or Render
settings below were exercised from this development VM** (no external accounts may be touched). The workflows,
scripts and policy were verified locally (actionlint, unit tests of the deploy helpers,
`deploy_render.py --dry-run`, a `--provenance=false` push to a throw-away local registry plus label reading).
Where this guide relies on documentation of GitHub or Render rather than on something observed, it says so.
Render facts were read from Render's current documentation on 2026-10-10 (sources at the end); things the
documentation did not confirm are collected in [section 7](#7-what-could-not-be-confirmed).

Names used throughout (replace if your account differs):

| Thing | Value |
|---|---|
| GitHub repository | `sixseven-pranav-jothivel/devops-demo` (the configured `origin`) |
| Image repository | `ghcr.io/sixseven-pranav-jothivel/stockflow` (owner **lowercase**) |
| Render service | `stockflow` (production); optionally `stockflow-demo` for the unsafe-deploy demo |
| Required check | `quality-gate` (only) |
| Environments | `production`, `demo-unsafe` |

## 1. GitHub repository

### 1.1 Create and push

1. Create the repository `devops-demo` under the account. Decide visibility first: the image package must be
   public for Render to pull it anonymously, and GitHub's availability of branch protection and
   environment required reviewers depends on plan and visibility (as documented by GitHub: generally available
   for public repositories on all plans, and for private repositories on paid plans; check your plan).
2. Push the code. Nothing has been committed from this VM; commit and push yourself:
   `git add -A && git commit -m "..." && git push -u origin main`. The first push to `main` starts `ci` and
   `release`; read section 4 first: with no Render configuration yet, that first `release` run is red by design (it publishes the image, then the mandatory deploy fails as not configured).

### 1.2 Actions permissions

Settings > Actions > General:

* **Actions permissions**: allow actions. All third-party actions are pinned to full commit SHAs, so "Allow
  GitHub-owned and verified creators" or "Allow all" both work.
* **Workflow permissions**: *Read repository contents and packages permissions* (the read-only default). The
  workflows request what they need per job (`packages: write` only in the `publish` job of `release.yml`).
* Leave **Allow GitHub Actions to create and approve pull requests** unchecked.
* **Fork pull request workflows**: require approval for first-time contributors (or all outside collaborators).
  `ci.yml` uses no secrets and never `pull_request_target`, so it is fork-safe either way.

### 1.3 Branch protection (require ONLY `quality-gate`)

Settings > Branches (or Rules > Rulesets) > add a rule for `main`:

* Require a pull request before merging. Set required approvals to **0** if you are the only person (otherwise
  you cannot merge your own demo PR).
* **Require status checks to pass before merging**: add exactly one check, **`quality-gate`** (choose it from the
  list of checks, source GitHub Actions).
* Do **not** add `lint-format`, `unit-tests`, etc. The `quality-gate` job depends on all of them and passes only
  when each is exactly `success` (skipped, failed, cancelled and missing all fail it), so one required check is
  both sufficient and robust against renaming jobs.
* Do not require linear history or signed commits unless you want to (not needed by the demo).

GitHub only offers a check name in this picker after it has run at least once in the repository, so create the
protection after the first CI run (section 4 step 1).

### 1.4 Environments

Settings > Environments > New environment.

**`production`** (used by the `deploy` job of `release.yml` and the `rollback` job of `rollback.yml`)

* Required reviewers: add yourself (and a colleague if available). With "Prevent self-review" off, a single
  presenter can approve their own run; each production deploy then pauses until someone clicks *Approve and
  deploy* (budget about 20 seconds in the timeline).
* Deployment branches: restrict to `main`.
* Environment **secret** `RENDER_DEPLOY_HOOK_URL`: the deploy hook URL of the Render service (section 3.4).
* Optional environment secrets `RENDER_API_KEY` and `RENDER_SERVICE_ID` (`srv-...`). With both present,
  `scripts/deploy/deploy_render.py` also polls the Render API and waits for the deploy to be reported
  `live` (or `failed`/`canceled`); without them the workflow infers the result from `/api/version` and the smoke
  test. Both are optional.

**`demo-unsafe`** (used only by `demo-unsafe-deploy.yml`)

* **Required reviewers: yes, mandatory in spirit.** This environment exists to put a human between a button and a
  deliberately broken deploy; the workflow header requires it.
* Deployment branches: `main` only.
* Environment secret `RENDER_DEPLOY_HOOK_URL` and environment variable `STOCKFLOW_BASE_URL` for a **separate demo
  service** if you can (section 3.5). If you point this environment at the production service, production will
  serve the broken image until you run `rollback.yml`.

### 1.5 Variables

Settings > Secrets and variables > Actions > **Variables** (repository level):

| Name | Value | Used by |
|---|---|---|
| `STOCKFLOW_BASE_URL` | `https://<your-service>.onrender.com` (https, no path) | `release.yml`, `rollback.yml`, `demo-unsafe-deploy.yml`, `perf.yml` |

It must be a **repository-level** variable because `perf.yml` runs without an environment. An environment-level
variable of the same name (set on `demo-unsafe`) overrides it for jobs in that environment. The preflight step
refuses a value that does not start with `http://` or `https://`.

No other variables or secrets are used. In particular there is no registry credential for Render (the package
is public) and the workflows push to GHCR with the per-job `GITHUB_TOKEN`.

### 1.6 GHCR package visibility

The image name is created by the first successful `publish` job. Then:

1. Your profile or organisation > Packages > `stockflow` > **Package settings**.
2. Danger Zone > **Change visibility** > **Public** and type the package name to confirm.
   GitHub documents that this **cannot be undone** and that a new package is private by default.
3. Package settings > **Manage Actions access**: make sure the `devops-demo` repository has *Write* access so
   later `release.yml` runs can push new tags. The image carries the label
   `org.opencontainers.image.source` (set by the build, from `IMAGE_SOURCE`), which GitHub uses to link the
   package to the repository when it is first published; GitHub documents the link as automatic in that case,
   but this was not observed here.

If a push from the workflow ever fails with 403/denied, step 3 is the first place to look (see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md)).

## 2. Local prerequisites for the demo machine

```bash
export PATH=$HOME/.local/bin:$PATH
make tools          # pinned, checksum-verified trivy, actionlint, k6 (only if missing)
make sync && make quality
make demo-fixtures  # needs network
```

For the manual push of the "broken" demo image (stage 13 of the runbook) you also need a GitHub **personal
access token (classic)** with `write:packages` (and `read:packages`), because GHCR authenticates `docker login`
with a PAT; the workflow `GITHUB_TOKEN` is not available on your laptop. (GitHub's documentation states that
GitHub Packages supports only classic PATs for this; not verified here.) Keep it out of the repository and
shell history (`read -rs GHCR_TOKEN`).

## 3. Render

All steps in the Render dashboard (https://dashboard.render.com).

### 3.1 The one rule that matters: `imgURL`

Render never redeploys an image-backed service by itself when a new image appears. A deploy is started manually
or through the service's **deploy hook**. The hook URL accepts an optional `imgURL` query parameter naming the
tag or digest to deploy. Render's documentation: *every part of `imgURL` other than the tag or digest must
match the service's default image URL, or Render rejects the request* (HTTP 404 for an invalid `imgURL`, 400 per
the deploy-hook page). `scripts/deploy/deploy_render.py` sends
`imgURL=ghcr.io/<owner>/stockflow@sha256:<digest>` (URL-encoded). Therefore the service must be created with
the image URL

```
ghcr.io/sixseven-pranav-jothivel/stockflow:0.1.0
```

i.e. same registry (`ghcr.io`), same lowercase owner, same repository (`stockflow`). Only the tag or digest
part may differ. If the service was created with `docker.io/...`, an uppercase owner, or another repository
name, every deploy from the pipeline will be rejected. The `0.1.0` tag exists only after the first
`publish` job has run (section 4), because the service creation form verifies that Render can access the image.
The tag in the service settings is merely the default for manual deploys; pipeline deploys name a digest.
An `imgURL` deploy applies to that deploy only; the service's configured image URL does not change.

### 3.2 Create the service (dashboard)

1. **New +** > **Web Service** > under *Source Code* choose **Existing Image**.
2. Image URL: `ghcr.io/sixseven-pranav-jothivel/stockflow:0.1.0`. For a public GHCR image no credential is
   needed (Render: *"a public GitHub Container Registry image doesn't need a credential"*). Click **Connect**.
3. Name `stockflow`; region as you like (the Blueprint uses `oregon`); instance type **Free** if available for
   your account and image-backed services (see section 7).
4. Environment variables: `DEPLOY_ENVIRONMENT=production`, `REPOSITORY_BACKEND=memory`. (The image already
   defaults to these; setting them makes the intent visible.) Do **not** set `PORT`: Render supplies it (default
   10000 per Render's documentation) and the app binds to `$PORT` (verified in `make docker-test`, which runs the
   container with a non-default `PORT`).
5. **Settings > Health & Alerts > Health Check Path**: `/health`. Without it Render only probes the TCP port.
   A `2xx`/`3xx` answer is healthy; checks must answer within 5 seconds.
6. **Instances: exactly one.** The data is in memory; two instances would show different data. Free web
   services cannot scale beyond one instance anyway.
7. Auto-deploy: image-backed services do not redeploy on new images; there is nothing to disable.
8. Create the service. It deploys the `:0.1.0` tag once; keep the URL (`https://stockflow-xxxx.onrender.com`).

Optional Blueprint: [`render.yaml`](../render.yaml) contains the same settings (`runtime: image`,
`plan: free`, `region: oregon`, `numInstances: 1`, `healthCheckPath: /health`). Replace `OWNER` with
`sixseven-pranav-jothivel` before using it. Creating a Blueprint was not tried here.

### 3.3 What Render does during a deploy (from its documentation)

* A new instance gets **no traffic until its health check passes**; the previous instance keeps serving
  (zero-downtime deploy). If all new instances pass, traffic moves.
* If the new instances do not become healthy within **15 minutes**, Render **cancels the deploy** and keeps
  routing to the existing instance.
* Render's health-check page describes no automatic rollback of a deploy that went live. A bad image that still
  answers `/health` with 2xx/3xx goes live; recover with `rollback.yml`.
* On a running service, after 15 seconds of consecutive failed checks Render stops routing to the instance, and
  after 60 seconds it restarts it.
* Free web services spin down after **15 minutes** without inbound traffic and need about a minute to start on
  the next request; they share 750 free instance hours per workspace per month. Whether zero-downtime deploys
  apply to the free tier is not stated on those pages.

### 3.4 Deploy hook

Service > **Settings** > **Deploy Hook**: copy the URL (it contains a secret `key`). Store it **only** as the
`RENDER_DEPLOY_HOOK_URL` secret of the `production` environment. Treat it as a password: the workflows pass it
via `env:`, mask it, and never print it. If it leaks, **Regenerate Hook** in the dashboard and update the secret.
Response codes (Render): `200` deploy started, `202` queued behind a running deploy (only with the *Wait*
overlapping-deploy policy), `400` invalid `imgURL`/`ref`, `401` missing/invalid `key`, `404` not found, `405`
wrong method, `409` service suspended.

### 3.5 Optional: a second service for the unsafe-deploy demo

Repeat 3.2 with name `stockflow-demo` and the **same** image repository
(`ghcr.io/sixseven-pranav-jothivel/stockflow:0.1.0`; the `imgURL` rule applies to it too). Put its hook in the
`demo-unsafe` environment secret and its URL in the `demo-unsafe` environment variable `STOCKFLOW_BASE_URL`.
Free instances share the monthly instance hours, and each spins down independently.

### 3.6 Optional: API credentials

Account Settings > API Keys > create a key; the service id (`srv-...`) is in the service URL. Add as
`RENDER_API_KEY` and `RENDER_SERVICE_ID` to the environment secrets. Not required.

## 4. Bootstrap / first push and first release

**A push to `main` requires a verified deployment.** If the production secret `RENDER_DEPLOY_HOOK_URL` or the
variable `STOCKFLOW_BASE_URL` is missing, the `deploy` job fails (red) with the error title *Deployment is
mandatory but not configured* and the summary *DEPLOYMENT FAILED: not configured (deployment is mandatory)*.
Before that, the `build-scan` and `publish` jobs have already built, scanned and pushed the image to GHCR and
`release-record.json` has been uploaded, because the image has to exist before the Render service can be created
from it. So **the first push is red by design** until the setup below is complete; this is the intended
bootstrap, not a bug. The manual `release` run has an input `require_deploy` that defaults to **true**; setting
it to false is the only opt-out, it only warns, and the record then says `deployed=false`. A run is reported
"deployed and verified" only when: the hook accepted the request; (if `RENDER_API_KEY` and `RENDER_SERVICE_ID`
exist) Render reported that deploy `live` for the released digest; the smoke test with `--expect-sha` passed;
and `release_tools.py verify-deployment` found the release record, the `imgURL` digest sent to Render, and the
live `/api/version` in agreement on digest, commit, version and name.
(All of this is read from `release.yml` and `scripts/deploy/release_tools.py`; none of it was run on GitHub.)

The order matters because of two chicken-and-egg problems: the package must exist before it can be made public
and before a Render service can be created from it, and the workflows must have run before branch protection can
name `quality-gate`.

1. **Push `main` with no Render secrets configured.** `ci` runs; `release` builds, scans and **publishes** the
   image (`sha-<sha>` and `0.1.0` tags, digest captured, `release-record` artifact uploaded). The `deploy` job
   then **fails** as "not configured" (approve the `production` environment first if reviewers are required).
   This red run is expected.
2. **Create branch protection** requiring `quality-gate` (section 1.3) now that the check exists.
3. **Make the package public** (section 1.6) and verify `docker pull ghcr.io/sixseven-pranav-jothivel/stockflow:0.1.0`
   works from a machine that is **not** logged in to GHCR.
4. **Create the Render service** from `ghcr.io/sixseven-pranav-jothivel/stockflow:0.1.0` (section 3.2). Wait
   until it is live; open the URL (a free service may need about a minute to wake).
5. **Configure GitHub**: secret `RENDER_DEPLOY_HOOK_URL` (environment `production`), variable
   `STOCKFLOW_BASE_URL` (repository), optional API secrets.
6. **Recover the failed run**, by either path:
   * *Re-run failed jobs* on the original `release` run: it redeploys the **already-published digest** (no rebuild);
     or
   * Actions > release > *Run workflow* on `main` (workflow_dispatch; `require_deploy` is true by default): this
     runs the whole chain again for the current `main`.
   Approve the `production` environment when asked. Do not set `require_deploy` to false to make it green.
7. Verify (section 5). Then open a PR with a trivial change, watch `quality-gate` go green, merge, and watch the
   whole chain run for the second time with a different commit sha.

Every later push to `main` (including every merged PR) runs the whole chain and must end with a verified deployment.

## 5. Verification steps

After a release run:

```bash
# 1. What is serving and which commit?
curl -s https://<service>.onrender.com/api/version
#   {"name":"stockflow","version":"0.1.0","git_sha":"<40 hex>","environment":"production",
#    "build_time":"...","repository":"memory","persistent":false}
# git_sha must equal the commit of the release run (github.sha).

# 2. The same checks the pipeline ran, from your machine
make smoke BASE_URL=https://<service>.onrender.com EXPECT_SHA=<commit sha>
#   expect six [PASS] lines and SMOKE TEST PASSED (format shown in README.md)
```

(The shape of both outputs was verified against a local run and the Docker container; the production values
are what you should see once deployed, not something observed here.)

In GitHub: the `release` run summary names the image `ghcr.io/sixseven-pranav-jothivel/stockflow@sha256:...`;
artifacts `release-record` and `deployment-record` (90 days) hold digest, version, sha and outcome. In Render:
the service's *Events* tab shows the deploy triggered by the hook. Keep the digest of each good release: it is
what `rollback.yml` takes (`sha256:<64 hex>`).

Dry-run the deploy helper without touching anything (verified locally with a fake hook URL):

```
$ RENDER_DEPLOY_HOOK_URL='https://api.render.com/deploy/srv-xxxx?key=...' \
    uv run python scripts/deploy/deploy_render.py --dry-run \
    --image ghcr.io/sixseven-pranav-jothivel/stockflow@sha256:<64 hex> --expect-owner sixseven-pranav-jothivel
dry run: would deploy ghcr.io/sixseven-pranav-jothivel/stockflow@sha256:...
::add-mask::https://api.render.com/deploy/srv-xxxx?key=...
dry run: hook url (redacted): ***&imgURL=ghcr.io%2Fsixseven-pranav-jothivel%2Fstockflow%40sha256%3A...
```

## 6. Pre-demo warm-up of the free tier

A free instance that has been idle for 15 minutes is asleep; the first request takes about a minute and
the dashboard data resets whenever the service restarts. About 10 minutes before the demo run
`make smoke BASE_URL=... ` (it also wakes the service) and keep the Render dashboard tab open. Do not run the
smoke test or `perf.yml` while the audience is looking at product data you care about: both write.

## 7. What could not be confirmed

* Whether Render's **Free** instance type can be selected for an image-backed (Existing Image) web service: the
  Free-instances page lists web services generally and does not mention Docker images; a web search suggested
  it is possible. If Free is not offered, choose the cheapest paid type or deploy a different type; nothing in
  the pipeline depends on the plan, but `render.yaml` says `plan: free`.
* Whether the free tier gets zero-downtime deploys (not stated on the pages read).
* Render's behaviour when a container **exits** at startup (the `bad_deployment` scenario): the documentation
  describes the 15-minute health-check window; whether Render fails such a deploy sooner is not documented on
  the pages read.
* GitHub: exact UI wording and plan limits for environments and branch protection on private repositories; the
  automatic repository link of a package published with the `org.opencontainers.image.source` label; PAT-only
  authentication for `docker login ghcr.io`. These are stated from GitHub's documentation or general knowledge,
  not observed.
* PR previews (section 8): whether a preview of a **free** base service may itself use the free plan; whether
  Render's preview API accepts an `imagePath` in the `repo@sha256:<digest>` form (its documentation says only
  that the tag or SHA may vary); how preview URLs are named; and that previews inherit the base service's
  environment variables (documented by Render; implies a preview can report `DEPLOY_ENVIRONMENT=production`).
* Everything in sections 1, 3 and 4 as a live sequence: no GitHub repository setting, GHCR package, Render
  service, hook call or environment approval was exercised.

Sources (read 2026-10-10): Render docs on deploying an image (`render.com/docs/deploy-an-image`), deploy hooks
(`render.com/docs/deploy-hooks`), health checks (`render.com/docs/health-checks`) and free instances
(`render.com/docs/free`); GitHub docs on configuring a package's access control and visibility.

## 8. Optional: per-PR preview environments

**NOT VERIFIED.** The workflow [`pr-preview.yml`](../.github/workflows/pr-preview.yml) and its scripts
(`scripts/deploy/preview_tools.py`, `preview_lib.py`) are covered by local unit tests only; nothing has run against
GitHub or Render. Skip this whole section if you do not want previews; nothing else depends on it.

### 8.1 What it does

A pull request that (a) comes from a branch in **this** repository (never a fork, never Dependabot) and (b)
carries the label `preview` gets a Render **image preview** of the production service:

1. `decide` (no secrets, no PR code) reads the event and picks `deploy`, `cleanup` or `skip`.
2. `gate` waits (up to 25 minutes) for a green `quality-gate` check run on the PR's exact head commit.
3. `build-scan` checks out the PR head (no secrets), builds linux/amd64, runs the container smoke test and
   image guards, applies the Trivy image gate, and saves the tarball with its sha256.
4. `publish` (`packages: write`, no secrets, no PR code) verifies the tarball, pushes
   `ghcr.io/<owner>/stockflow:pr-<n>` and `:pr-<n>-<sha7>`, checks both resolve to one digest and that the OCI labels
   name the head commit.
5. `preview` (environment `preview`; scripts from the trusted base branch) creates the preview with
   `POST /v1/services/{id}/preview` (base service = `RENDER_SERVICE_ID`) **by digest**, or redeploys it on later
   pushes, waits until that deploy is live, and smoke-tests it with `--expect-sha` = the PR head commit.
6. `report` writes one sticky PR comment with status and URL.
7. On PR close or when the label is removed, `cleanup` deletes the preview service (`stockflow-pr-<n>`); a missing
   one is fine. Render never deletes image previews by itself; if cleanup fails the comment names the service so
   you can delete it manually.

A labelled PR on a repository where the secrets are missing fails with **`NOT DEPLOYED: preview not configured`**.
A PR with a red `quality-gate` gets the comment "No preview" instead of a deployment.

### 8.2 Set it up

1. **Create the label** (exact name `preview`):
   `gh label create preview --description "Deploy a Render preview for this PR" --color 1d76db --repo sixseven-pranav-jothivel/devops-demo`
   (or Issues > Labels > New label). Not run here.
2. **Create the environment** `preview`: Settings > Environments > New environment. Strongly recommended: add
   **required reviewers**. The workflow header documents the residual risk: with a `pull_request` trigger the
   workflow file itself comes from the PR merge commit, so anyone who can push a branch to this repository can
   alter it, and the `preview` environment is what holds the Render API key. Reviewers on the environment make a
   human approve each job that can read the key; also restrict who has write access.
3. **Set the secrets** on that environment (the values are never echoed; paste at the prompt):
   ```bash
   gh secret set RENDER_API_KEY    --env preview --repo sixseven-pranav-jothivel/devops-demo
   gh secret set RENDER_SERVICE_ID --env preview --repo sixseven-pranav-jothivel/devops-demo   # srv-... of the BASE (production) service
   ```
   Optional variable (leave unset unless Render rejects the default plan):
   `gh variable set PREVIEW_PLAN --env preview --body <plan> --repo sixseven-pranav-jothivel/devops-demo`.
   `RENDER_SERVICE_ID` must be an image-backed service whose image URL is `ghcr.io/sixseven-pranav-jothivel/stockflow`
   (the production service from section 3). Render's rule is that a preview's image path may differ only in tag or
   digest.
4. **API key risk.** Unlike the deploy hook, which can only redeploy one service, a Render API key is an account
   credential: the script uses it to create and delete services (previews), and a leaked key can do whatever the
   key's owner can (the exact scope of a Render API key was not looked up here; check Render's current
   documentation and use the least-privileged account or workspace you can). Keep it only in the `preview`
   environment secret (not repository secrets), rotate it after demos, and revoke it if the repository's write
   access list changes. The same `RENDER_API_KEY` secret name in the `production` environment is a separate,
   optional secret used only to poll deploy status.
5. **GHCR**: the preview tags go into the same public package; Render pulls the digest anonymously.
6. **Cost and plan.** Each preview is an extra Render service billed by plan. Whether a preview of a free base
   service can be on the free plan is **not confirmed**; Render documents that previews of a base service on a
   paid plan cannot use the free plan. If creation fails on the plan, set `PREVIEW_PLAN`.

### 8.3 How to try it (NOT VERIFIED; do it once before relying on it)

1. Push a branch from this repository with a trivial change and open a PR into `main`.
2. Add the `preview` label. The `pr-preview` run starts; wait for `quality-gate`, then the build, publish and
   `preview` jobs. With no secrets yet configured this ends in `NOT DEPLOYED: preview not configured`: that is the
   expected unconfigured result.
3. After configuring the secrets, re-run failed jobs: the sticky comment should show `DEPLOYED and verified`, the
   commit, the image digest and a preview URL (an `onrender.com` address; the URL naming is unconfirmed). Open it
   and check `/api/version` shows the PR head sha.
4. Push another commit: the same preview is redeployed to the new digest.
5. Remove the label or close the PR: `cleanup` runs, the comment says `Preview deleted`, and the service
   `stockflow-pr-<n>` disappears from the Render dashboard (what to expect, not observed).
