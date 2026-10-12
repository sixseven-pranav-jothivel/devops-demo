# ruff: noqa: E501  (long human-readable messages are clearer unwrapped)
"""Per-pull-request preview environments on Render (image previews). Standard library only.

Facts below come from Render's OpenAPI document (https://api-docs.render.com/openapi/render-public-api-1.json,
fetched 2026-10-11) and https://render.com/docs/service-previews. Anything the documents do not state is
marked UNCONFIRMED and the code fails loudly instead of assuming.

* ``POST /v1/services/{serviceId}/preview`` (operationId ``preview-service``) creates a preview of an
  IMAGE-BACKED base service. Body: ``imagePath`` (required; a full URL or a path relative to the base
  service's image; "the path must match (only the tag or SHA can vary)"), ``name`` (optional), ``plan``
  (optional; omitted = the base service's plan). 200 response: ``{"service": {...}, "deployId": "..."}``;
  ``service`` has ``id``, ``name``, ``dashboardUrl`` and ``serviceDetails.url``.
* There is no "update preview" or "list previews" endpoint. A preview is a service: list with
  ``GET /v1/services?name=<name>&includePreviews=true``, redeploy it with ``POST /v1/services/{id}/deploys``
  ``{"imageUrl": ...}`` ("host, repository, and image name must match the currently configured image"),
  delete it with ``DELETE /v1/services/{id}`` (204). Render does NOT delete image previews by itself.
* Plan: "If your base service uses a paid compute plan, its previews can't use the Free compute plan."
  Whether a preview of a FREE base service may itself be Free is UNCONFIRMED (the docs only say image
  previews are billed by the plan in the request); ``PREVIEW_PLAN`` therefore stays optional and unset.
* Previews copy the base service's settings, including env vars (``DEPLOY_ENVIRONMENT`` would still read
  ``production``). UNCONFIRMED: whether ``imagePath`` accepts the ``repo@sha256:digest`` form (the docs say
  "tag or SHA"); the same rule is documented for the deploy hook, which the pipeline already uses.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import deploy_lib as lib

PREVIEW_LABEL = "preview"
COMMENT_MARKER = "<!-- stockflow-preview -->"
GATE_CHECK = "quality-gate"
GITHUB_API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
PLAN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
PREVIEW_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.onrender\.com$")
DEPENDABOT = "dependabot[bot]"
BOT_LOGIN = "github-actions[bot]"  # the only author whose marker comment we will edit


# --------------------------------------------------------------------------- who gets a preview
@dataclass(frozen=True)
class Decision:
    action: str  # deploy | cleanup | skip
    reason: str
    pr_number: int = 0
    head_sha: str = ""


def _ineligible(env: Mapping[str, str]) -> str | None:
    """Why this pull request can never get a preview (``None`` = eligible)."""
    repo = env.get("GITHUB_REPOSITORY", "")
    number = env.get("PR_NUMBER", "")
    if env.get("GITHUB_EVENT_NAME", "") != "pull_request":
        return f"event {env.get('GITHUB_EVENT_NAME') or '?'} is not pull_request"
    if not REPO_RE.match(repo) or env.get("PR_HEAD_REPO", "") != repo:
        return "pull request does not come from this repository (fork)"
    if DEPENDABOT in (env.get("PR_AUTHOR", ""), env.get("GITHUB_ACTOR", "")):
        return "Dependabot pull requests never get a preview"
    if not number.isdigit() or int(number) < 1:
        return "invalid pull request number"
    if not lib.SHA_RE.match(env.get("PR_HEAD_SHA", "")):
        return "invalid head sha"
    return None


def decide(env: Mapping[str, str]) -> Decision:
    """The script-level guard (the workflow has a job-level ``if`` too). Deny by default.

    Input is the ``pull_request`` event passed through environment variables, never interpolated into
    a command line. Only same-repository, non-Dependabot PRs carrying the ``preview`` label qualify.
    """
    problem = _ineligible(env)
    if problem is not None:
        return Decision("skip", problem)
    number, sha = int(env["PR_NUMBER"]), env["PR_HEAD_SHA"]
    action = env.get("EVENT_ACTION", "")
    on_label = env.get("EVENT_LABEL", "") == PREVIEW_LABEL
    has_label = PREVIEW_LABEL in _labels(env.get("PR_LABELS", "[]"))
    # (action, condition) -> (decision, reason); anything not listed is skipped
    table: dict[str, tuple[bool, str, str, str]] = {
        # always: deleting is idempotent and a label removed earlier must never leave a preview behind
        "closed": (True, "cleanup", "pull request closed", ""),
        "unlabeled": (
            on_label,
            "cleanup",
            "preview label removed",
            "a different label was removed",
        ),
        "labeled": (on_label, "deploy", "preview label added", "a different label was added"),
        "synchronize": (has_label, "deploy", "pull request synchronized", "no preview label"),
        "reopened": (has_label, "deploy", "pull request reopened", "no preview label"),
    }
    if action not in table:
        return Decision("skip", f"unsupported action {action or '?'}", number, sha)
    matches, result, why, why_not = table[action]
    return Decision(result, why, number, sha) if matches else Decision("skip", why_not, number, sha)


def _labels(raw: str) -> set[str]:
    try:
        data = json.loads(raw)
    except ValueError:
        return set()
    return {x for x in data if isinstance(x, str)} if isinstance(data, list) else set()


def preview_name(pr_number: int) -> str:
    if pr_number < 1:
        raise lib.DeployError("invalid pull request number")
    return f"stockflow-pr-{pr_number}"


def image_tags(owner: str, pr_number: int, sha: str) -> list[str]:
    """``pr-<n>`` and ``pr-<n>-<sha7>`` tags (informational; the preview is deployed by digest)."""
    if not lib.SHA_RE.match(sha):
        raise lib.DeployError("invalid sha")
    repo = lib.build_image_ref(owner, "sha256:" + "0" * 64).split("@", maxsplit=1)[0]
    return [f"{repo}:pr-{pr_number}", f"{repo}:pr-{pr_number}-{sha[:7]}"]


# --------------------------------------------------------------------------- Render previews
@dataclass(frozen=True)
class RenderService:
    service_id: str
    name: str
    url: str | None


def _service(node: Any) -> RenderService | None:
    svc = node.get("service", node) if isinstance(node, dict) else None
    if not isinstance(svc, dict):
        return None
    sid, name = svc.get("id"), svc.get("name")
    if not isinstance(sid, str) or not isinstance(name, str):
        return None
    details = svc.get("serviceDetails")
    url = details.get("url") if isinstance(details, dict) else None
    return RenderService(sid, name, url if isinstance(url, str) else None)


def validate_preview_url(url: str) -> str:
    """The URL Render reports must be https and under onrender.com (documented: previews get their own
    onrender.com URL); anything else is refused, because it ends up in a PR comment and is smoke-tested."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname or not PREVIEW_HOST_RE.match(parts.hostname):
        raise lib.DeployError(
            "Render reported a preview URL that is not https://<name>.onrender.com"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.port or parts.username:
        raise lib.DeployError("Render reported an unexpected preview URL shape")
    return f"https://{parts.hostname}"


class PreviewApi(lib.RenderApi):
    """Create, find, redeploy and delete image previews of the base service ``service_id``."""

    def find(self, name: str) -> RenderService | None:
        query = urllib.parse.urlencode({"name": name, "includePreviews": "true", "limit": "100"})
        data = self._request("GET", f"/services?{query}")
        found = [
            s
            for s in (_service(item) for item in (data if isinstance(data, list) else []))
            if s is not None and s.name == name and s.service_id != self._service
        ]
        if len(found) > 1:
            raise lib.DeployError(
                f"{len(found)} Render services are named {name!r}; refusing to guess"
            )
        return found[0] if found else None

    def create(
        self, image_ref: str, name: str, plan: str | None = None
    ) -> tuple[RenderService, str | None]:
        lib.validate_image_ref(image_ref)
        body: dict[str, Any] = {"imagePath": image_ref, "name": name}
        if plan:
            if not PLAN_RE.match(plan):
                raise lib.DeployError("PREVIEW_PLAN has an unexpected format")
            body["plan"] = plan
        data = self._request("POST", f"/services/{self._service}/preview", body)
        service = _service(data)
        deploy_id = data.get("deployId") if isinstance(data, dict) else None
        if service is None:
            raise lib.DeployError("Render API returned an unexpected body for the new preview")
        return service, deploy_id if isinstance(deploy_id, str) else None

    def redeploy(self, service_id: str, image_ref: str) -> str | None:
        """Deploy a new image to an existing preview; returns the deploy id (``None`` if queued)."""
        lib.validate_image_ref(image_ref)
        self._check_id(service_id)
        data = self._request("POST", f"/services/{service_id}/deploys", {"imageUrl": image_ref})
        deploy_id = data.get("id") if isinstance(data, dict) else None
        return deploy_id if isinstance(deploy_id, str) else None

    def retrieve(self, service_id: str) -> RenderService:
        self._check_id(service_id)
        service = _service(self._request("GET", f"/services/{service_id}"))
        if service is None:
            raise lib.DeployError("Render API returned an unexpected service body")
        return service

    def delete(self, service_id: str) -> bool:
        """Delete a preview. ``False`` if it was already gone (404/410); the base service is untouchable."""
        self._check_id(service_id)
        if service_id == self._service:
            raise lib.DeployError("refusing to delete the base service")
        try:
            self._request("DELETE", f"/services/{service_id}")
        except lib.RenderHttpError as exc:
            if exc.code in (404, 410):
                return False
            raise
        return True

    @staticmethod
    def _check_id(service_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", service_id):
            raise lib.DeployError("unexpected Render service id format")


@dataclass(frozen=True)
class PreviewResult:
    service_id: str
    deploy_id: str | None
    url: str
    created: bool


def deploy_preview(
    api: PreviewApi,
    *,
    key: str,
    image_ref: str,
    pr_number: int,
    plan: str | None,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> PreviewResult:
    """Create the PR's preview (or redeploy it) by digest and wait until THIS deploy is ``live``."""
    _, digest = lib.validate_image_ref(image_ref)
    name = preview_name(pr_number)
    existing = api.find(name)
    known_ids: list[str] = []
    if existing is None:
        service, deploy_id = api.create(image_ref, name, plan)
        log(f"created preview service {name} ({service.service_id})")
        created = True
    else:
        service = existing
        # deploys that already exist must not be mistaken for ours (a re-run can reuse the same digest)
        known_ids = [
            d.deploy_id for d in PreviewApi(key, service.service_id, api.opener).list_deploys()
        ]
        deploy_id = api.redeploy(existing.service_id, image_ref)
        log(f"redeployed existing preview service {name} ({service.service_id})")
        created = False
    watcher = PreviewApi(key, service.service_id, api.opener)
    outcome = lib.wait_for_render_deploy(
        watcher,
        digest=digest,
        deploy_id=deploy_id,
        known_ids=known_ids,
        timeout=timeout,
        interval=interval,
        sleep=sleep,
        now=now,
        log=log,
    )
    if not outcome.ok:
        raise lib.DeployError(f"preview deploy did not go live: {outcome.status}")
    url = service.url or api.retrieve(service.service_id).url
    if not url:
        raise lib.DeployError("Render did not report a URL for the preview")
    return PreviewResult(service.service_id, outcome.deploy_id, validate_preview_url(url), created)


# --------------------------------------------------------------------------- GitHub (check runs, comments)
class GitHubApi:
    """Minimal GitHub REST client: https only, no redirects, token never logged."""

    def __init__(self, token: str, repo: str, opener: lib.Opener = lib.default_opener) -> None:
        if not REPO_RE.match(repo):
            raise lib.DeployError("invalid repository name")
        self._token, self._repo, self._opener = token, repo, opener

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        where = f"{method} {path.split('?', maxsplit=1)[0]}"
        request = urllib.request.Request(f"{GITHUB_API}{path}", data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "stockflow-preview/1")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener(request, timeout=30.0) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise lib.DeployError(f"GitHub API {where} answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise lib.DeployError(
                f"GitHub API {where} failed: {lib.redact(str(getattr(exc, 'reason', exc)), [self._token])}"
            ) from None
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError:
            raise lib.DeployError(f"GitHub API {where} returned a body that is not JSON") from None

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        if not lib.SHA_RE.match(sha):
            raise lib.DeployError("invalid sha")
        data = self._request(
            "GET", f"/repos/{self._repo}/commits/{sha}/check-runs?per_page=100&filter=latest"
        )
        runs = data.get("check_runs") if isinstance(data, dict) else None
        return [r for r in (runs or []) if isinstance(r, dict)]

    def pull(self, pr_number: int) -> dict[str, Any]:
        data = self._request("GET", f"/repos/{self._repo}/pulls/{pr_number}")
        if not isinstance(data, dict):
            raise lib.DeployError("GitHub API returned an unexpected pull request body")
        return data

    def find_comment(self, pr_number: int) -> int | None:
        for page in range(1, 11):  # 1000 comments is more than enough; refuse to loop forever
            items = self._request(
                "GET", f"/repos/{self._repo}/issues/{pr_number}/comments?per_page=100&page={page}"
            )
            if not isinstance(items, list) or not items:
                return None
            for item in items:
                if not isinstance(item, dict):
                    continue
                user = item.get("user") or {}
                if (
                    COMMENT_MARKER in str(item.get("body", ""))
                    and user.get("type") == "Bot"
                    and user.get("login") == BOT_LOGIN
                    and isinstance(item.get("id"), int)
                ):
                    return int(item["id"])
        return None

    def upsert_comment(self, pr_number: int, body: str) -> str:
        existing = self.find_comment(pr_number)
        if existing is None:
            self._request(
                "POST", f"/repos/{self._repo}/issues/{pr_number}/comments", {"body": body}
            )
            return "created"
        self._request("PATCH", f"/repos/{self._repo}/issues/comments/{existing}", {"body": body})
        return "updated"


def pr_state(pr: Mapping[str, Any], head_sha: str) -> tuple[str, str]:
    """``(code, reason)``: ``ok`` | ``closed`` | ``unlabeled`` | ``sha_changed``.

    A preview may only exist while the PR is open, still carries the ``preview`` label and its head is
    still the commit that was built. ``closed`` and ``unlabeled`` mean the preview must not exist.
    """
    if pr.get("state") != "open":
        return "closed", "the pull request was closed while the preview was being deployed"
    labels = {x.get("name") for x in (pr.get("labels") or []) if isinstance(x, dict)}
    if PREVIEW_LABEL not in labels:
        return "unlabeled", "the preview label was removed while the preview was being deployed"
    head = (pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None
    if head != head_sha:
        return "sha_changed", "a newer commit was pushed; the newer run owns the preview"
    return "ok", "the pull request is open, labelled and unchanged"


def gate_state(runs: Sequence[Mapping[str, Any]]) -> str:
    """``success`` | ``failure`` | ``pending`` | ``missing`` for the ``quality-gate`` check run.

    Only a check run created by the GitHub Actions app counts (a third-party app could otherwise post a
    green check with the same name). With several runs of that name the newest one decides.
    """
    mine = [
        r
        for r in runs
        if r.get("name") == GATE_CHECK and (r.get("app") or {}).get("slug") == "github-actions"
    ]
    if not mine:
        return "missing"
    latest = max(mine, key=lambda r: (str(r.get("started_at") or ""), int(r.get("id") or 0)))
    if latest.get("status") != "completed":
        return "pending"
    return "success" if latest.get("conclusion") == "success" else "failure"


def wait_for_gate(
    fetch: Callable[[], Sequence[Mapping[str, Any]]],
    *,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> str:
    """Poll until ``quality-gate`` completes. Returns success/failure, or ``timeout``."""
    deadline = now() + timeout
    while True:
        state = gate_state(fetch())
        log(f"quality-gate: {state}")
        if state in ("success", "failure"):
            return state
        if now() >= deadline:
            return "timeout"
        sleep(min(interval, max(deadline - now(), 0)))


# --------------------------------------------------------------------------- sticky comment
def _cleanup_state(
    result: Callable[[str], str], output: Callable[[str, str], str]
) -> dict[str, str]:
    if result("cleanup") != "success":
        return {
            "status": "cleanup_failed",
            "headline": "Preview cleanup FAILED",
            "detail": f"Delete the Render service {{name}} by hand. Job result: {result('cleanup')}.",
        }
    if output("cleanup", "deleted") != "true":
        return {
            "status": "nothing_to_delete",
            "headline": "Preview deleted",
            "detail": "No preview was running for this pull request.",
        }
    return {
        "status": "deleted",
        "headline": "Preview deleted",
        "detail": "The preview service was removed.",
    }


def _early_failure(gate: str, build: str, publish: str) -> dict[str, str] | None:
    if gate != "success":
        why = {
            "failure": "quality-gate FAILED for this commit",
            "cancelled": "the run was cancelled (a newer push superseded it)",
        }.get(gate, f"quality-gate did not pass ({gate})")
        return {"status": "no_preview", "headline": "No preview", "detail": why}
    if build != "success" or publish != "success":
        return {
            "status": "build_failed",
            "headline": "No preview: image build, scan or publish failed",
            "detail": f"build-scan: {build}, publish: {publish}",
        }
    return None


def derive_state(needs: Mapping[str, Any], decision_action: str) -> dict[str, str]:
    """Turn the ``needs`` context of the report job into a verdict that never over-claims."""

    def result(job: str) -> str:
        entry = needs.get(job)
        value = entry.get("result") if isinstance(entry, dict) else None
        return value if isinstance(value, str) else "missing"

    def output(job: str, key: str) -> str:
        entry = needs.get(job)
        outputs = entry.get("outputs") if isinstance(entry, dict) else None
        value = outputs.get(key) if isinstance(outputs, dict) else None
        return value if isinstance(value, str) else ""

    if decision_action == "cleanup":
        return _cleanup_state(result, output)
    gate, build, publish, preview = (
        result("gate"),
        result("build-scan"),
        result("publish"),
        result("preview"),
    )
    early = _early_failure(gate, build, publish)
    if early is not None:
        return early
    if preview == "success" and output("preview", "superseded") == "true":
        return {
            "status": "superseded",
            "headline": "No preview: the pull request changed while it was being deployed",
            "detail": output("preview", "superseded_reason") or "see the run",
        }
    if preview == "success":
        return {
            "status": "deployed",
            "headline": "DEPLOYED and verified",
            "detail": "",
            "url": output("preview", "url"),
            "digest": output("publish", "digest"),
        }
    if output("preview", "configured") == "false":
        return {
            "status": "not_configured",
            "headline": "NOT DEPLOYED: preview not configured",
            "detail": "Set RENDER_API_KEY and RENDER_SERVICE_ID in the `preview` environment "
            "(docs/SETUP_GITHUB_RENDER.md).",
        }
    return {
        "status": "failed",
        "headline": "Preview deployment FAILED",
        "detail": f"preview job result: {preview}. The image was published but no preview is serving.",
        "digest": output("publish", "digest"),
    }


def render_comment(state: Mapping[str, str], *, pr_number: int, head_sha: str, run_url: str) -> str:
    """Markdown for the sticky PR comment. Every value goes through ``md_safe``."""
    safe = lib.md_safe
    lines = [
        COMMENT_MARKER,
        f"### Preview environment: {safe(state['headline'])}",
        "",
        f"- commit: `{safe(head_sha[:12])}`",
        f"- status: `{safe(state['status'])}`",
    ]
    url = state.get("url", "")
    if state["status"] == "deployed" and url:
        try:
            lines.append(f"- URL: {validate_preview_url(url)}")
        except lib.DeployError:
            lines.append("- URL: (withheld: not a valid onrender.com address)")
    if state.get("digest"):
        lines.append(f"- image digest: `{safe(state['digest'], 100)}`")
    if state.get("detail"):
        detail = safe(state["detail"], 300).replace("{name}", f"`{preview_name(pr_number)}`")
        lines.append(f"- {detail}")
    if state["status"] == "deployed":
        lines.append(
            "- note: data is in-memory and the service is a Render preview; it is deleted when the "
            "PR is closed or the `preview` label is removed."
        )
    if run_url:
        lines.append(f"- run: {safe(run_url, 300)}")
    return "\n".join(lines) + "\n"
