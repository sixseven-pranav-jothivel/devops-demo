# ruff: noqa: E501  (long human-readable messages are clearer unwrapped)
"""Shared, dependency-free helpers for the StockFlow release, rollback and demo-deploy workflows.

Everything here is a pure function or takes its collaborators (HTTP opener, clock, sleep) as arguments, so
``tests/deploy`` exercises it without a network. Standard library only.

Security rules implemented here (docs/SECURITY.md sections 5 and 10.4):

* the Render deploy hook URL contains the hook key, so it is read from the environment only, is never
  placed in an exception message or log line (``redact``), and is only used over HTTPS;
* the image that is deployed must match ``ghcr.io/<owner>/stockflow@sha256:<64 hex>`` exactly, so a
  mutable tag or another repository can never be sent to the hook;
* the optional Render API key is sent only to ``https://api.render.com``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

IMAGE_NAME = "stockflow"
REGISTRY = "ghcr.io"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OWNER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,38})$")
IMAGE_REF_RE = re.compile(
    rf"^{re.escape(REGISTRY)}/(?P<owner>[a-z0-9](?:[a-z0-9._-]{{0,38}}))/{IMAGE_NAME}"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
RENDER_API = "https://api.render.com/v1"
MASK = "***"

# Render deploy statuses (https://api-docs.render.com/reference/list-deploys).
RENDER_SUCCESS = frozenset({"live"})
RENDER_FAILURE = frozenset(
    {"build_failed", "update_failed", "canceled", "pre_deploy_failed", "deactivated"}
)
RENDER_PENDING = frozenset(
    {"created", "queued", "build_in_progress", "update_in_progress", "pre_deploy_in_progress"}
)


class DeployError(Exception):
    """An operator-facing failure. The message never contains a secret."""


# --------------------------------------------------------------------------- image references
def validate_digest(digest: str) -> str:
    digest = digest.strip()
    if not DIGEST_RE.match(digest):
        raise DeployError("image digest must look like sha256:<64 lowercase hex characters>")
    return digest


def validate_image_ref(ref: str, expected_owner: str | None = None) -> tuple[str, str]:
    """Return ``(owner, digest)`` for ``ghcr.io/<owner>/stockflow@sha256:<64hex>`` or raise."""
    match = IMAGE_REF_RE.match(ref.strip())
    if not match:
        raise DeployError(
            f"image must match {REGISTRY}/<owner>/{IMAGE_NAME}@sha256:<64 hex> "
            "(a digest reference; tags are refused because they are mutable)"
        )
    owner = match.group("owner")
    if expected_owner is not None and owner != expected_owner.lower():
        raise DeployError(
            f"image owner {owner!r} is not the expected owner {expected_owner.lower()!r}"
        )
    return owner, match.group("digest")


def build_image_ref(owner: str, digest: str) -> str:
    owner = owner.strip().lower()
    if not OWNER_RE.match(owner):
        raise DeployError(f"invalid registry owner {owner!r}")
    return f"{REGISTRY}/{owner}/{IMAGE_NAME}@{validate_digest(digest)}"


def parse_push_digest(output: str) -> str:
    """Extract the digest from ``docker push`` output (``... digest: sha256:<hex> size: N``)."""
    found = re.findall(r"digest:\s*(sha256:[0-9a-f]{64})", output)
    if not found:
        raise DeployError("could not find 'digest: sha256:<hex>' in docker push output")
    if len(set(found)) != 1:
        raise DeployError(f"docker push reported several different digests: {sorted(set(found))}")
    return found[0]


# --------------------------------------------------------------------------- secrets
def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every non-trivial secret (and its URL-encoded form) in ``text`` with ``***``."""
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, MASK).replace(urllib.parse.quote(secret, safe=""), MASK)
    return text


def hook_secrets(hook_url: str, image_ref: str | None = None) -> list[str]:
    """Everything derived from the hook that must never be printed: the URL, the URL with imgURL,
    and each query value (the hook key) on its own, so a partial leak is masked too."""
    secrets = [hook_url]
    if image_ref is not None:
        secrets.append(build_hook_url(hook_url, image_ref))
    query = urllib.parse.urlsplit(hook_url).query
    secrets += [v for _, v in urllib.parse.parse_qsl(query) if len(v) >= 4]
    return secrets


def github_mask_commands(secrets: Iterable[str]) -> list[str]:
    """``::add-mask::`` lines so the runner also masks values derived from a secret."""
    return [f"::add-mask::{s}" for s in secrets if s]


def build_hook_url(hook_url: str, image_ref: str) -> str:
    """Return ``hook_url`` with ``imgURL=<url-encoded image_ref>`` set (any existing imgURL replaced)."""
    parts = urllib.parse.urlsplit(hook_url.strip())
    if parts.scheme != "https" or not parts.netloc:
        raise DeployError("the deploy hook must be an https:// URL")
    pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k != "imgURL"
    ]
    pairs.append(("imgURL", image_ref))
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe="")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


# --------------------------------------------------------------------------- HTTP plumbing
class Opener(Protocol):
    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Any: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would re-send headers such as ``Authorization`` (and the query
    string carrying the deploy-hook key) to whatever host the response names."""

    def redirect_request(  # type: ignore[override]  # noqa: PLR0917
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        fp.close()  # do not leak the redirect response's socket
        raise DeployError(f"refusing to follow an HTTP {code} redirect (target not shown)")


def open_no_redirect(request: urllib.request.Request, *, timeout: float) -> Any:
    """``urlopen`` that raises ``DeployError`` instead of following redirects."""
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def default_opener(request: urllib.request.Request, *, timeout: float) -> Any:
    """Opener for the authenticated Render API and the deploy hook: https only, no redirects."""
    if urllib.parse.urlsplit(request.full_url).scheme != "https":
        raise DeployError("refusing a non-https request")
    return open_no_redirect(request, timeout=timeout)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_base_url(url: str) -> str:
    """The deployed service URL: https://, or http:// for loopback hosts only (local walkthroughs)."""
    parts = urllib.parse.urlsplit(url.strip())
    if not parts.netloc or not parts.hostname:
        raise DeployError("base url must be an https:// URL")
    if parts.scheme == "https":
        return url.strip().rstrip("/")
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return url.strip().rstrip("/")
    raise DeployError(
        "base url must be an https:// URL (http:// is accepted only for localhost, 127.0.0.1 or [::1])"
    )


@dataclass(frozen=True)
class HookResult:
    status: int
    deploy_id: str | None
    queued: bool  # HTTP 202: another deploy is running and the workspace policy is "wait"


def parse_deploy_id(body: str) -> str | None:
    """Deploy id from a deploy-hook response body (``{"deploy": {"id": ...}}`` or ``{"id": ...}``)."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict):
        candidates = [
            data.get("id"),
            (data.get("deploy") or {}).get("id") if isinstance(data.get("deploy"), dict) else None,
        ]
        for value in candidates:
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{3,64}", value):
                return value
    return None


def trigger_hook(
    hook_url: str,
    image_ref: str,
    *,
    opener: Opener = default_opener,
    timeout: float = 30.0,
) -> HookResult:
    """POST the deploy hook for ``image_ref``. Failures raise ``DeployError`` without the URL."""
    url = build_hook_url(hook_url, image_ref)
    secrets = [hook_url, url]
    request = urllib.request.Request(url, data=b"", method="POST")
    request.add_header("User-Agent", "stockflow-deploy/1")
    try:
        with opener(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise DeployError(
            f"deploy hook rejected the request: HTTP {exc.code} ({_http_hint(exc.code)})"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = redact(str(getattr(exc, "reason", exc)), secrets)
        raise DeployError(f"deploy hook request failed: {reason}") from None
    if status not in (200, 202):
        raise DeployError(f"deploy hook answered HTTP {status}, expected 200 or 202")
    return HookResult(status=status, deploy_id=parse_deploy_id(body), queued=status == 202)


def _http_hint(code: int) -> str:
    return {
        400: "invalid imgURL: its registry/repository must equal the service's image URL",
        401: "hook key invalid or missing",
        404: "service not found",
        405: "method not allowed",
        409: "service suspended or deploys blocked",
    }.get(code, "see Render deploy hook docs")


# --------------------------------------------------------------------------- Render API (optional)
@dataclass(frozen=True)
class RenderDeploy:
    deploy_id: str
    status: str
    image_ref: str | None
    image_sha: str | None


class RenderApi:
    """Read-only client for the documented deploy endpoints (needs RENDER_API_KEY + service id)."""

    def __init__(self, api_key: str, service_id: str, opener: Opener = default_opener) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", service_id):
            raise DeployError("RENDER_SERVICE_ID has an unexpected format")
        self._key = api_key
        self._service = service_id
        self._opener = opener

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(f"{RENDER_API}{path}", method="GET")
        request.add_header("Authorization", f"Bearer {self._key}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "stockflow-deploy/1")
        try:
            with self._opener(request, timeout=30.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise DeployError(f"Render API {path} answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DeployError(
                f"Render API {path} failed: {redact(str(getattr(exc, 'reason', exc)), [self._key])}"
            ) from None

    @staticmethod
    def _parse(item: Any) -> RenderDeploy | None:
        deploy = item.get("deploy", item) if isinstance(item, dict) else None
        if not isinstance(deploy, dict) or not isinstance(deploy.get("id"), str):
            return None
        image = deploy.get("image") if isinstance(deploy.get("image"), dict) else {}
        return RenderDeploy(
            deploy_id=deploy["id"],
            status=str(deploy.get("status", "")),
            image_ref=image.get("ref"),
            image_sha=image.get("sha"),
        )

    def list_deploys(self, limit: int = 20) -> list[RenderDeploy]:
        data = self._get(f"/services/{self._service}/deploys?limit={int(limit)}")
        parsed = [self._parse(item) for item in (data if isinstance(data, list) else [])]
        return [d for d in parsed if d is not None]

    def get_deploy(self, deploy_id: str) -> RenderDeploy:
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", deploy_id):
            raise DeployError("unexpected Render deploy id format")
        deploy = self._parse(self._get(f"/services/{self._service}/deploys/{deploy_id}"))
        if deploy is None:
            raise DeployError("Render API returned an unexpected deploy body")
        return deploy

    def live_image_sha(self) -> str | None:
        """Digest of the image currently live (newest deploy in status ``live``), if known."""
        for deploy in self.list_deploys():
            if deploy.status == "live" and deploy.image_sha:
                return deploy.image_sha
        return None

    def find_deploy_for_digest(self, digest: str, since: Sequence[str] = ()) -> RenderDeploy | None:
        """Newest deploy of ``digest`` that is not in ``since`` (ids that existed before the trigger)."""
        for deploy in self.list_deploys():
            if deploy.deploy_id in since:
                continue
            if digest in (deploy.image_sha or "") or digest in (deploy.image_ref or ""):
                return deploy
        return None


@dataclass(frozen=True)
class RenderOutcome:
    deploy_id: str | None
    status: str  # a Render status, "timeout" or "not_found"
    ok: bool


def wait_for_render_deploy(
    api: RenderApi,
    *,
    digest: str,
    deploy_id: str | None,
    known_ids: Sequence[str],
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> RenderOutcome:
    """Poll Render until the deploy is ``live`` (ok), failed/canceled (not ok) or ``timeout`` expires."""
    deadline = now() + timeout
    while True:
        try:
            deploy = (
                api.get_deploy(deploy_id)
                if deploy_id
                else api.find_deploy_for_digest(digest, known_ids)
            )
        except DeployError as exc:  # transient API trouble: keep polling until the deadline
            log(f"  render api: {exc}")
            deploy = None
        if deploy is not None:
            deploy_id = deploy.deploy_id
            log(f"  render deploy {deploy_id}: {deploy.status}")
            if deploy.image_ref and digest not in deploy.image_ref:
                log(f"  render deploy {deploy_id} is not for this image (ref {deploy.image_ref})")
                return RenderOutcome(deploy_id, "image_mismatch", False)
            if deploy.status in RENDER_SUCCESS:
                return RenderOutcome(deploy_id, deploy.status, True)
            if deploy.status in RENDER_FAILURE:
                return RenderOutcome(deploy_id, deploy.status, False)
        else:
            log("  render deploy not visible yet")
        if now() >= deadline:
            return RenderOutcome(deploy_id, "timeout" if deploy_id else "not_found", False)
        sleep(min(interval, max(deadline - now(), 0)))


# --------------------------------------------------------------------------- observation
def fetch_version(
    base_url: str, *, opener: Callable[..., Any] = open_no_redirect, timeout: float = 10.0
) -> dict[str, Any] | None:
    """Best-effort ``GET /api/version`` for reporting (never used as a gate). ``None`` if unreachable."""
    base_url = validate_base_url(base_url)
    request = urllib.request.Request(base_url + "/api/version", method="GET")
    request.add_header("User-Agent", "stockflow-deploy/1")
    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, DeployError):
        return None  # includes a refused redirect: unreachable as far as this report is concerned
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- quality gate
def evaluate_needs(
    needs: Mapping[str, Any], required: Sequence[str] = ()
) -> tuple[bool, list[tuple[str, str]]]:
    """``needs`` is ``toJSON(needs)``. Passes only if every job (and every required name) is ``success``."""
    rows: list[tuple[str, str]] = []
    for name in sorted(set(needs) | set(required)):
        entry = needs.get(name)
        result = entry.get("result") if isinstance(entry, dict) else None
        rows.append((name, result if isinstance(result, str) else "missing"))
    ok = bool(rows) and all(result == "success" for _, result in rows)
    return ok, rows


# --------------------------------------------------------------------------- preflight
def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def preflight(env: Mapping[str, str]) -> dict[str, Any]:
    """What can this run actually do? Inputs are booleans/URLs, never the secrets themselves."""
    missing: list[str] = []
    if not _truthy(env.get("HOOK_CONFIGURED")):
        missing.append(
            "secret RENDER_DEPLOY_HOOK_URL (an environment secret of the `production` environment)"
        )
    base = (env.get("STOCKFLOW_BASE_URL") or "").strip()
    if not base:
        missing.append(
            "variable STOCKFLOW_BASE_URL (repository or `production` environment variable)"
        )
    else:
        try:
            validate_base_url(base)
        except DeployError:
            missing.append(
                "variable STOCKFLOW_BASE_URL must be an https:// URL "
                "(http:// is accepted only for localhost, 127.0.0.1 or [::1])"
            )
    return {
        "configured": not missing,
        "missing": missing,
        "render_api": _truthy(env.get("RENDER_API_CONFIGURED")),
        "base_url": base.rstrip("/"),
        "required": _truthy(env.get("DEPLOY_REQUIRED")),
    }


# --------------------------------------------------------------------------- end-to-end verification
def verify_deployment(
    *,
    release_record: Mapping[str, Any],
    sent_image: str,
    expect_sha: str,
    expect_version: str,
    observed: Mapping[str, Any] | None,
) -> list[str]:
    """Problems (empty list == verified) comparing what was released, what was sent, what is serving.

    * the digest in release-record.json == the digest in the imgURL that was sent to the hook;
    * the record names this commit and version;
    * the live ``/api/version`` reports this commit's sha and the released version.
    """
    problems: list[str] = []
    _, sent_digest = validate_image_ref(sent_image)
    if release_record.get("digest") != sent_digest:
        problems.append(
            f"digest mismatch: release record has {release_record.get('digest')!r}, "
            f"the deploy hook was sent {sent_digest!r}"
        )
    if release_record.get("image") != sent_image:
        problems.append("image reference sent to the hook differs from release-record.json")
    if release_record.get("git_sha") != expect_sha:
        problems.append(
            f"release record is for commit {release_record.get('git_sha')!r}, not {expect_sha!r}"
        )
    if release_record.get("version") != expect_version:
        problems.append(
            f"release record version {release_record.get('version')!r} != {expect_version!r}"
        )
    if observed is None:
        problems.append("the deployed service did not answer /api/version")
        return problems
    if observed.get("git_sha") != expect_sha:
        problems.append(
            f"deployed git_sha is {observed.get('git_sha')!r}, expected {expect_sha!r} "
            "(an older or different build is serving)"
        )
    if observed.get("version") != expect_version:
        problems.append(
            f"deployed version is {observed.get('version')!r}, expected {expect_version!r}"
        )
    if observed.get("name") != "stockflow":
        problems.append(f"deployed name is {observed.get('name')!r}, expected 'stockflow'")
    return problems


# --------------------------------------------------------------------------- records
def classify_deployment(
    *, configured: bool, deploy_outcome: str, verify_outcome: str
) -> tuple[str, bool, bool]:
    """Return ``(status, deployed, verified)`` from GitHub step outcomes (success/failure/skipped...)."""
    if not configured:
        return "not_deployed_unconfigured", False, False
    if deploy_outcome != "success":
        return "failed_deploy", False, False
    if verify_outcome != "success":
        return "deployed_unverified", True, False
    return "deployed_verified", True, True


def build_record(
    env: Mapping[str, str],
    *,
    kind: str,
    image: str,
    version: str,
    git_sha: str,
    tags: Sequence[str],
    status: str,
    deployed: bool,
    verified: bool,
    base_url: str,
    deploy_result: Mapping[str, Any] | None = None,
    reason: str = "",
    required: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    owner, digest = validate_image_ref(image)
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    result = dict(deploy_result or {})
    return {
        "schema": 1,
        "kind": kind,
        "timestamp": (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": repo,
        "git_sha": git_sha,
        "version": version,
        "image": image,
        "owner": owner,
        "digest": digest,
        "tags": list(tags),
        "workflow": {
            "name": env.get("GITHUB_WORKFLOW", ""),
            "run_id": run_id,
            "run_attempt": env.get("GITHUB_RUN_ATTEMPT", ""),
            "run_url": f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else "",
            "actor": env.get("GITHUB_ACTOR", ""),
            "ref": env.get("GITHUB_REF", ""),
        },
        "deployed": deployed,
        "verified": verified,
        "deploy_required": required,
        "deployment": {
            "status": status,
            "hook_http_status": result.get("hook_http_status"),
            "render_deploy_id": result.get("render_deploy_id"),
            "render_status": result.get("render_status"),
            "previous_image_sha": result.get("previous_image_sha"),
        },
        "verification": {"base_url": base_url, "smoke": "pass" if verified else "not_passed"},
        "reason": reason,
    }


def build_publish_record(
    env: Mapping[str, str],
    *,
    image: str,
    version: str,
    git_sha: str,
    tags: Sequence[str],
    build_time: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """``release-record.json``: the immutable identity of a published image (the rollback source)."""
    owner, digest = validate_image_ref(image)
    if not SHA_RE.match(git_sha):
        raise DeployError("git sha must be 40 lowercase hex characters")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    return {
        "schema": 1,
        "kind": "release-publish",
        "published_at": (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": repo,
        "git_sha": git_sha,
        "version": version,
        "build_time": build_time,
        "image": image,
        "owner": owner,
        "digest": digest,
        "tags": list(tags),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else "",
        "rollback_hint": f"run the rollback workflow with image_digest={digest}",
    }


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_MARKUP_RE = re.compile(r"[`<>|\[\]\\]")


def md_safe(value: object, limit: int = 200) -> str:
    """Make free text safe for one cell of a GitHub step-summary table.

    Control characters and newlines become a space (no extra rows/blocks), and backticks, ``<``/``>``
    (HTML), ``|`` (cell break), ``[``/``]`` (links) and backslashes are removed. ``@`` is replaced so user
    and team mentions cannot ping anyone. The result is cut to ``limit`` characters.
    """
    text = _CONTROL_RE.sub(" ", str(value))
    text = _MARKUP_RE.sub("", text).replace("@", "(at)")
    return " ".join(text.split())[:limit]


def render_summary(record: Mapping[str, Any]) -> str:
    """Markdown for ``$GITHUB_STEP_SUMMARY``. Honest about deployments that did not happen."""
    dep = record["deployment"]
    status = dep["status"]
    headline = {
        "deployed_verified": "DEPLOYED and verified",
        "deployed_unverified": "Deployed, VERIFICATION FAILED",
        "failed_deploy": "Deployment FAILED",
        "not_deployed_unconfigured": "NOT DEPLOYED: live deployment is not configured"
        + (" (mandatory: this run FAILED)" if record.get("deploy_required") else ""),
    }.get(status, md_safe(status))
    tags = ", ".join(f"`{md_safe(t)}`" for t in record["tags"]) or "-"
    lines = [
        f"## {md_safe(record['kind']).capitalize()}: {headline}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| deployed | **{str(record['deployed']).lower()}** |",
        f"| verified (health + ready + version + smoke) | **{str(record['verified']).lower()}** |",
        f"| status | `{md_safe(status)}` |",
        f"| image | `{md_safe(record['image'], 300)}` |",
        f"| version / git sha | `{md_safe(record['version'])}` / `{md_safe(record['git_sha'])}` |",
        f"| tags | {tags} |",
        f"| render deploy | `{md_safe(dep.get('render_deploy_id') or '-')}` "
        f"({md_safe(dep.get('render_status') or 'not polled')}) |",
        f"| previous image | `{md_safe(dep.get('previous_image_sha') or 'unknown')}` |",
        f"| target | {md_safe(record['verification']['base_url'] or '-')} |",
        f"| run | {md_safe(record['workflow']['run_url'] or '-')} |",
    ]
    if record.get("reason"):
        lines.append(f"| reason | {md_safe(record['reason'])} |")
    if status == "not_deployed_unconfigured":
        lines += [
            "",
            "> **The image was built, scanned and published to GHCR, but nothing was deployed.** "
            "Create the Render service, set the `production` environment secret `RENDER_DEPLOY_HOOK_URL` and "
            "the variable `STOCKFLOW_BASE_URL` (see docs/SETUP_GITHUB_RENDER.md), then re-run this "
            "workflow with `workflow_dispatch` (require_deploy = true).",
        ]
    return "\n".join(lines) + "\n"


def image_labels(inspect_json: Any) -> dict[str, str]:
    """OCI labels from ``docker buildx imagetools inspect --format '{{json .Image}}'`` output."""
    node = inspect_json
    if isinstance(node, dict) and "config" not in node and "Config" not in node:
        # multi-platform form: {"linux/amd64": {...}}
        preferred = node.get("linux/amd64") or next(iter(node.values()), None)
        node = preferred
    if not isinstance(node, dict):
        raise DeployError("unexpected image inspect output")
    config = node.get("config") or node.get("Config") or {}
    labels = config.get("Labels") or config.get("labels") or {}
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}


def release_identity(labels: Mapping[str, str]) -> tuple[str, str]:
    """``(version, git_sha)`` baked into an image; refuses images without a full 40-hex revision."""
    version = labels.get("org.opencontainers.image.version", "")
    sha = labels.get("org.opencontainers.image.revision", "")
    if not version or not SHA_RE.match(sha):
        raise DeployError(
            "image has no usable org.opencontainers.image.version / .revision labels "
            "(not a StockFlow release image?)"
        )
    return version, sha
