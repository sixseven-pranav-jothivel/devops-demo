"""Per-PR preview environments: guard, gate, Render preview client, GitHub comment, CLI."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import deploy_lib as lib
import preview_lib as pv
import preview_tools
import pytest

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
IMAGE = f"ghcr.io/octo-org/stockflow@{DIGEST}"
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "octo-org/stockflow-demo"


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = "") -> None:
        self.status = status
        self._body = (body if isinstance(body, str) else json.dumps(body)).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class Script:
    """Opener answering from a list of (status|exception, body); records every request."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "err", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        return FakeResponse(200, answer)

    def body(self, index: int = -1) -> dict[str, Any]:
        data = self.requests[index].data
        assert isinstance(data, bytes)
        return json.loads(data)


def event(**override: str) -> dict[str, str]:
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_ACTOR": "octocat",
        "EVENT_ACTION": "labeled",
        "EVENT_LABEL": "preview",
        "PR_NUMBER": "12",
        "PR_HEAD_SHA": SHA,
        "PR_HEAD_REPO": REPO,
        "PR_AUTHOR": "octocat",
        "PR_LABELS": json.dumps(["preview"]),
    }
    env.update(override)
    return env


# --------------------------------------------------------------------------- who gets a preview
@pytest.mark.parametrize(
    ("override", "action"),
    [
        ({}, "deploy"),
        ({"EVENT_ACTION": "synchronize", "EVENT_LABEL": ""}, "deploy"),
        ({"EVENT_ACTION": "reopened", "EVENT_LABEL": ""}, "deploy"),
        ({"EVENT_ACTION": "closed", "EVENT_LABEL": ""}, "cleanup"),
        ({"EVENT_ACTION": "unlabeled", "PR_LABELS": "[]"}, "cleanup"),
        # not opted in / irrelevant events
        ({"EVENT_ACTION": "synchronize", "PR_LABELS": "[]"}, "skip"),
        ({"EVENT_ACTION": "reopened", "PR_LABELS": "[]"}, "skip"),
        ({"EVENT_ACTION": "labeled", "EVENT_LABEL": "bug"}, "skip"),
        ({"EVENT_ACTION": "unlabeled", "EVENT_LABEL": "bug"}, "skip"),
        ({"EVENT_ACTION": "closed", "PR_LABELS": "[]"}, "cleanup"),  # always: delete is idempotent
        ({"EVENT_ACTION": "edited"}, "skip"),
        ({"EVENT_ACTION": ""}, "skip"),
        # never: forks, Dependabot, other events, malformed data
        ({"PR_HEAD_REPO": "evil/stockflow-demo"}, "skip"),
        ({"PR_HEAD_REPO": ""}, "skip"),
        ({"PR_AUTHOR": "dependabot[bot]"}, "skip"),
        ({"GITHUB_ACTOR": "dependabot[bot]"}, "skip"),
        ({"GITHUB_EVENT_NAME": "pull_request_target"}, "skip"),
        ({"GITHUB_EVENT_NAME": "push"}, "skip"),
        ({"PR_NUMBER": "0"}, "skip"),
        ({"PR_NUMBER": "12; rm -rf /"}, "skip"),
        ({"PR_NUMBER": ""}, "skip"),
        ({"PR_HEAD_SHA": "abc"}, "skip"),
        ({"PR_HEAD_SHA": SHA + "\nx"}, "skip"),
        ({"GITHUB_REPOSITORY": "not a repo", "PR_HEAD_REPO": "not a repo"}, "skip"),
        ({"EVENT_ACTION": "synchronize", "PR_LABELS": "not json"}, "skip"),
    ],
)
def test_decide_matrix(override: dict[str, str], action: str) -> None:
    assert pv.decide(event(**override)).action == action


def test_decide_carries_validated_number_and_sha() -> None:
    decision = pv.decide(event())
    assert (decision.pr_number, decision.head_sha) == (12, SHA)


def test_a_fork_that_names_its_label_preview_still_gets_nothing() -> None:
    env = event(PR_HEAD_REPO="fork-owner/stockflow-demo", PR_LABELS=json.dumps(["preview"]))
    assert pv.decide(env).action == "skip"


def test_names_and_tags() -> None:
    assert pv.preview_name(7) == "stockflow-pr-7"
    with pytest.raises(lib.DeployError):
        pv.preview_name(0)
    assert pv.image_tags("Octo-Org", 7, SHA) == [
        "ghcr.io/octo-org/stockflow:pr-7",
        "ghcr.io/octo-org/stockflow:pr-7-0123456",
    ]
    with pytest.raises(lib.DeployError):
        pv.image_tags("octo-org", 7, "short")


# --------------------------------------------------------------------------- the quality-gate wait
def run(
    name: str, status: str, conclusion: str | None, slug: str = "github-actions", n: int = 1
) -> dict:
    return {
        "id": n,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": f"2026-10-10T10:00:0{n}Z",
        "app": {"slug": slug},
    }


def test_gate_state_values() -> None:
    assert pv.gate_state([]) == "missing"
    assert pv.gate_state([run("lint", "completed", "success")]) == "missing"
    assert pv.gate_state([run("quality-gate", "in_progress", None)]) == "pending"
    assert pv.gate_state([run("quality-gate", "queued", None)]) == "pending"
    assert pv.gate_state([run("quality-gate", "completed", "success")]) == "success"
    for bad in ("failure", "cancelled", "skipped", "timed_out", "neutral"):
        assert pv.gate_state([run("quality-gate", "completed", bad)]) == "failure"


def test_gate_ignores_a_check_posted_by_another_app() -> None:
    fake = run("quality-gate", "completed", "success", slug="evil-app")
    assert pv.gate_state([fake]) == "missing"
    real_failed = run("quality-gate", "completed", "failure", n=1)
    assert pv.gate_state([fake, real_failed]) == "failure"


def test_gate_newest_run_decides() -> None:
    old_red = run("quality-gate", "completed", "failure", n=1)
    new_green = run("quality-gate", "completed", "success", n=2)
    assert pv.gate_state([old_red, new_green]) == "success"
    assert pv.gate_state([new_green, run("quality-gate", "in_progress", None, n=3)]) == "pending"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_wait_for_gate_polls_until_green() -> None:
    answers = iter(
        [
            [],
            [run("quality-gate", "in_progress", None)],
            [run("quality-gate", "completed", "success")],
        ]
    )
    clock = Clock()
    result = pv.wait_for_gate(
        lambda: next(answers),
        timeout=300,
        interval=20,
        sleep=clock.sleep,
        now=clock.now,
        log=lambda m: None,
    )
    assert result == "success"
    assert clock.t == 40


def test_wait_for_gate_stops_on_red_and_times_out() -> None:
    clock = Clock()
    red = [run("quality-gate", "completed", "failure")]
    assert (
        pv.wait_for_gate(
            lambda: red,
            timeout=300,
            interval=20,
            sleep=clock.sleep,
            now=clock.now,
            log=lambda m: None,
        )
        == "failure"
    )
    clock2 = Clock()
    assert (
        pv.wait_for_gate(
            lambda: [],
            timeout=60,
            interval=20,
            sleep=clock2.sleep,
            now=clock2.now,
            log=lambda m: None,
        )
        == "timeout"
    )


# --------------------------------------------------------------------------- preview urls
@pytest.mark.parametrize(
    "url", ["https://stockflow-pr-12-abcd.onrender.com", "https://x1.onrender.com/"]
)
def test_preview_url_accepts_onrender(url: str) -> None:
    assert pv.validate_preview_url(url).startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "http://stockflow.onrender.com",
        "https://evil.example",
        "https://onrender.com.evil.example",
        "https://stockflow.onrender.com@evil.example",
        "https://stockflow.onrender.com:8443",
        "https://stockflow.onrender.com/path?x=1",
        "https://user:pw@stockflow.onrender.com",
        "javascript:alert(1)",
        "",
    ],
)
def test_preview_url_rejects_everything_else(url: str) -> None:
    with pytest.raises(lib.DeployError):
        pv.validate_preview_url(url)


# --------------------------------------------------------------------------- Render preview client
def svc(sid: str, name: str, url: str | None = "https://p.onrender.com") -> dict[str, Any]:
    return {"id": sid, "name": name, "serviceDetails": {"url": url}}


def test_create_posts_documented_body_to_the_documented_path() -> None:
    script = Script({"service": svc("srv-new1", "stockflow-pr-12"), "deployId": "dep-aaa"})
    api = pv.PreviewApi("KEY-123456", "srv-base1", script)
    service, deploy_id = api.create(IMAGE, "stockflow-pr-12")
    request = script.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.render.com/v1/services/srv-base1/preview"
    assert request.get_header("Authorization") == "Bearer KEY-123456"
    assert script.body() == {"imagePath": IMAGE, "name": "stockflow-pr-12"}  # no plan unless asked
    assert (service.service_id, deploy_id) == ("srv-new1", "dep-aaa")


def test_create_sends_plan_only_when_given_and_valid() -> None:
    script = Script({"service": svc("srv-new1", "n"), "deployId": "dep-aaa"})
    pv.PreviewApi("k" * 8, "srv-base1", script).create(IMAGE, "n", "starter")
    assert script.body()["plan"] == "starter"
    with pytest.raises(lib.DeployError):
        pv.PreviewApi("k" * 8, "srv-base1", script).create(IMAGE, "n", "free; DROP")


def test_create_refuses_a_tag_and_a_malformed_response() -> None:
    api = pv.PreviewApi("k" * 8, "srv-base1", Script({"nope": 1}))
    with pytest.raises(lib.DeployError):
        api.create("ghcr.io/octo-org/stockflow:latest", "n")
    with pytest.raises(lib.DeployError, match="unexpected body"):
        api.create(IMAGE, "n")


def test_find_matches_the_exact_name_only_and_never_the_base_service() -> None:
    listing = [
        {"service": svc("srv-other", "stockflow-pr-123"), "cursor": "c"},
        {"service": svc("srv-base1", "stockflow-pr-12"), "cursor": "c"},
        {"service": svc("srv-mine1", "stockflow-pr-12"), "cursor": "c"},
    ]
    script = Script(listing)
    found = pv.PreviewApi("k" * 8, "srv-base1", script).find("stockflow-pr-12")
    assert found is not None
    assert found.service_id == "srv-mine1"
    url = script.requests[0].full_url
    assert "includePreviews=true" in url
    assert "name=stockflow-pr-12" in url


def test_find_none_and_ambiguous() -> None:
    assert pv.PreviewApi("k" * 8, "srv-base1", Script([])).find("stockflow-pr-12") is None
    two = [
        {"service": svc("srv-a11", "stockflow-pr-12")},
        {"service": svc("srv-b22", "stockflow-pr-12")},
    ]
    with pytest.raises(lib.DeployError, match="refusing to guess"):
        pv.PreviewApi("k" * 8, "srv-base1", Script(two)).find("stockflow-pr-12")


def test_redeploy_posts_image_url() -> None:
    script = Script({"id": "dep-bbb"})
    assert pv.PreviewApi("k" * 8, "srv-base1", script).redeploy("srv-mine1", IMAGE) == "dep-bbb"
    assert script.requests[0].full_url == "https://api.render.com/v1/services/srv-mine1/deploys"
    assert script.body() == {"imageUrl": IMAGE}
    queued = Script("")  # 202 with no body: queued, no id
    assert pv.PreviewApi("k" * 8, "srv-base1", queued).redeploy("srv-mine1", IMAGE) is None


def test_delete_and_missing_and_base_protection() -> None:
    script = Script("")
    assert pv.PreviewApi("k" * 8, "srv-base1", script).delete("srv-mine1") is True
    assert script.requests[0].get_method() == "DELETE"
    for gone in (404, 410):
        assert pv.PreviewApi("k" * 8, "srv-base1", Script(gone)).delete("srv-mine1") is False
    with pytest.raises(lib.DeployError, match="HTTP 403"):
        pv.PreviewApi("k" * 8, "srv-base1", Script(403)).delete("srv-mine1")
    with pytest.raises(lib.DeployError, match="base service"):
        pv.PreviewApi("k" * 8, "srv-base1", Script("")).delete("srv-base1")
    with pytest.raises(lib.DeployError):
        pv.PreviewApi("k" * 8, "srv-base1", Script("")).delete("../x")


def test_http_errors_never_contain_the_key_or_the_query() -> None:
    api = pv.PreviewApi("KEY-123456", "srv-base1", Script(401))
    with pytest.raises(lib.DeployError) as info:
        api.find("stockflow-pr-12")
    assert "KEY-123456" not in str(info.value)
    assert "?" not in str(info.value)


# --------------------------------------------------------------------------- deploy_preview
def deploy_obj(deploy_id: str, status: str, digest: str = DIGEST) -> dict[str, Any]:
    return {
        "id": deploy_id,
        "status": status,
        "image": {"ref": f"ghcr.io/octo-org/stockflow@{digest}", "sha": digest},
    }


class Sequencer:
    """Routes by method+path, so the order of calls inside deploy_preview does not matter."""

    def __init__(self, routes: dict[str, list[Any]]) -> None:
        self.routes = routes
        self.seen: list[str] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        key = f"{request.get_method()} {request.full_url.split('/v1', 1)[1].split('?', 1)[0]}"
        self.seen.append(key)
        answers = self.routes[key]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "err", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        return FakeResponse(200, answer)


def call_deploy(opener: Sequencer, plan: str | None = None) -> pv.PreviewResult:
    clock = Clock()
    return pv.deploy_preview(
        pv.PreviewApi("KEY-123456", "srv-base1", opener),
        key="KEY-123456",
        image_ref=IMAGE,
        pr_number=12,
        plan=plan,
        timeout=100,
        interval=10,
        sleep=clock.sleep,
        now=clock.now,
        log=lambda m: None,
    )


def test_deploy_preview_creates_waits_and_returns_a_validated_url() -> None:
    opener = Sequencer(
        {
            "GET /services": [[]],
            "POST /services/srv-base1/preview": [
                {
                    "service": svc("srv-new1", "stockflow-pr-12", "https://sf-pr-12.onrender.com"),
                    "deployId": "dep-aaa",
                }
            ],
            "GET /services/srv-new1/deploys/dep-aaa": [
                deploy_obj("dep-aaa", "build_in_progress"),
                deploy_obj("dep-aaa", "live"),
            ],
        }
    )
    result = call_deploy(opener)
    assert result == pv.PreviewResult("srv-new1", "dep-aaa", "https://sf-pr-12.onrender.com", True)
    assert "DELETE /services/srv-new1" not in opener.seen


def test_deploy_preview_redeploys_an_existing_preview_and_ignores_old_deploys() -> None:
    opener = Sequencer(
        {
            "GET /services": [[{"service": svc("srv-mine1", "stockflow-pr-12")}]],
            "GET /services/srv-mine1/deploys": [[{"deploy": deploy_obj("dep-old", "live")}]],
            "POST /services/srv-mine1/deploys": [{"id": "dep-new"}],
            "GET /services/srv-mine1/deploys/dep-new": [deploy_obj("dep-new", "live")],
        }
    )
    result = call_deploy(opener)
    assert (result.created, result.deploy_id) == (False, "dep-new")


def test_deploy_preview_fails_loudly_when_the_deploy_fails_or_is_for_another_image() -> None:
    created = {"service": svc("srv-new1", "stockflow-pr-12"), "deployId": "dep-aaa"}
    failing = Sequencer(
        {
            "GET /services": [[]],
            "POST /services/srv-base1/preview": [created],
            "GET /services/srv-new1/deploys/dep-aaa": [deploy_obj("dep-aaa", "update_failed")],
        }
    )
    with pytest.raises(lib.DeployError, match="update_failed"):
        call_deploy(failing)
    wrong = Sequencer(
        {
            "GET /services": [[]],
            "POST /services/srv-base1/preview": [created],
            "GET /services/srv-new1/deploys/dep-aaa": [deploy_obj("dep-aaa", "live", OTHER)],
        }
    )
    with pytest.raises(lib.DeployError, match="image_mismatch"):
        call_deploy(wrong)


def test_deploy_preview_refuses_a_foreign_url() -> None:
    opener = Sequencer(
        {
            "GET /services": [[]],
            "POST /services/srv-base1/preview": [
                {
                    "service": svc("srv-new1", "stockflow-pr-12", "https://evil.example"),
                    "deployId": "dep-aaa",
                }
            ],
            "GET /services/srv-new1/deploys/dep-aaa": [deploy_obj("dep-aaa", "live")],
        }
    )
    with pytest.raises(lib.DeployError, match=r"onrender\.com"):
        call_deploy(opener)


# --------------------------------------------------------------------------- GitHub client
def test_check_runs_request_and_token_handling() -> None:
    script = Script({"check_runs": [run("quality-gate", "completed", "success")]})
    github = pv.GitHubApi("ghs_TOKEN123", REPO, script)
    assert len(github.check_runs(SHA)) == 1
    request = script.requests[0]
    assert (
        request.full_url
        == f"https://api.github.com/repos/{REPO}/commits/{SHA}/check-runs?per_page=100&filter=latest"
    )
    assert request.get_header("Authorization") == "Bearer ghs_TOKEN123"
    with pytest.raises(lib.DeployError):
        github.check_runs("not-a-sha")
    with pytest.raises(lib.DeployError):
        pv.GitHubApi("t", "bad repo")


def test_sticky_comment_is_created_then_updated_and_only_bot_comments_count() -> None:
    forged = {"id": 1, "body": pv.COMMENT_MARKER, "user": {"type": "User", "login": "mallory"}}
    mine = {
        "id": 2,
        "body": pv.COMMENT_MARKER + "old",
        "user": {"type": "Bot", "login": "github-actions[bot]"},
    }
    # page 1 -> forged only, page 2 -> empty: no sticky comment yet -> POST
    github = pv.GitHubApi("tok-123456", REPO, Script([forged], [], {}))
    assert github.upsert_comment(12, "body") == "created"
    script = Script([forged, mine], {})
    assert pv.GitHubApi("tok-123456", REPO, script).upsert_comment(12, "new body") == "updated"
    patch = script.requests[-1]
    assert patch.get_method() == "PATCH"
    assert patch.full_url.endswith("/issues/comments/2")


def test_another_bots_marker_comment_is_ignored_and_a_new_one_is_created() -> None:
    other_bot = {
        "id": 7,
        "body": pv.COMMENT_MARKER + "planted",
        "user": {"type": "Bot", "login": "renovate[bot]"},
    }
    script = Script([other_bot], [], {})
    assert pv.GitHubApi("tok-123456", REPO, script).upsert_comment(12, "body") == "created"
    assert script.requests[-1].get_method() == "POST"  # never PATCHed the other bot's comment
    assert not any(r.get_method() == "PATCH" for r in script.requests)
    assert pv.GitHubApi("tok-123456", REPO, Script([other_bot], [])).find_comment(12) is None


def test_github_errors_do_not_leak_the_token() -> None:
    with pytest.raises(lib.DeployError) as info:
        pv.GitHubApi("ghs_TOKEN123", REPO, Script(403)).check_runs(SHA)
    assert "ghs_TOKEN123" not in str(info.value)


# --------------------------------------------------------------------------- comment content
def needs(**jobs: tuple[str, dict[str, str]]) -> dict[str, Any]:
    return {name: {"result": res, "outputs": out} for name, (res, out) in jobs.items()}


GREEN = {
    "gate": ("success", {}),
    "build-scan": ("success", {}),
    "publish": ("success", {"digest": DIGEST, "image": IMAGE}),
    "preview": ("success", {"url": "https://sf-pr-12.onrender.com", "configured": "true"}),
}


def test_state_deployed_only_when_everything_succeeded() -> None:
    state = pv.derive_state(needs(**GREEN), "deploy")
    assert state["status"] == "deployed"
    assert state["headline"] == "DEPLOYED and verified"


def test_state_red_gate_gets_no_preview_with_the_reason() -> None:
    state = pv.derive_state(needs(**{**GREEN, "gate": ("failure", {})}), "deploy")
    assert state["status"] == "no_preview"
    assert "quality-gate FAILED" in state["detail"]
    assert (
        pv.derive_state(needs(**{**GREEN, "gate": ("skipped", {})}), "deploy")["status"]
        == "no_preview"
    )


def test_state_build_failure_and_unconfigured_and_deploy_failure() -> None:
    assert (
        pv.derive_state(needs(**{**GREEN, "build-scan": ("failure", {})}), "deploy")["status"]
        == "build_failed"
    )
    unconfigured = needs(**{**GREEN, "preview": ("failure", {"configured": "false"})})
    state = pv.derive_state(unconfigured, "deploy")
    assert state["status"] == "not_configured"
    assert state["headline"] == "NOT DEPLOYED: preview not configured"
    failed = needs(**{**GREEN, "preview": ("failure", {"configured": "true"})})
    assert pv.derive_state(failed, "deploy")["status"] == "failed"
    cancelled = needs(**{**GREEN, "preview": ("cancelled", {})})
    assert pv.derive_state(cancelled, "deploy")["status"] == "failed"


def test_state_cleanup() -> None:
    deleted = pv.derive_state(needs(cleanup=("success", {"deleted": "true"})), "cleanup")
    assert deleted["status"] == "deleted"
    nothing = pv.derive_state(needs(cleanup=("success", {"deleted": "false"})), "cleanup")
    assert nothing["status"] == "nothing_to_delete"
    unconfigured = pv.derive_state(needs(cleanup=("success", {})), "cleanup")
    assert unconfigured["status"] == "nothing_to_delete"
    assert pv.derive_state(needs(cleanup=("failure", {})), "cleanup")["status"] == "cleanup_failed"
    assert (
        pv.derive_state({}, "cleanup")["status"] == "cleanup_failed"
    )  # a missing job is not success


def test_state_superseded_is_never_reported_as_deployed() -> None:
    superseded = needs(
        **{
            **GREEN,
            "preview": (
                "success",
                {
                    "superseded": "true",
                    "superseded_reason": "the pull request was closed",
                    "url": "https://a.onrender.com",
                },
            ),
        }
    )
    state = pv.derive_state(superseded, "deploy")
    assert state["status"] == "superseded"
    assert "DEPLOYED" not in state["headline"]
    body = pv.render_comment(state, pr_number=12, head_sha=SHA, run_url="")
    assert "a.onrender.com" not in body  # the URL of a deleted preview is not advertised
    not_superseded = needs(
        **{
            **GREEN,
            "preview": ("success", {"superseded": "false", "url": "https://a.onrender.com"}),
        }
    )
    assert pv.derive_state(not_superseded, "deploy")["status"] == "deployed"


# ---- re-check of the PR state
def pr_json(
    state: str = "open", labels: tuple[str, ...] = ("preview",), sha: str = SHA
) -> dict[str, Any]:
    return {"state": state, "labels": [{"name": n} for n in labels], "head": {"sha": sha}}


@pytest.mark.parametrize(
    ("pr", "code"),
    [
        (pr_json(), "ok"),
        (pr_json(state="closed"), "closed"),
        (pr_json(labels=()), "unlabeled"),
        (pr_json(labels=("bug",)), "unlabeled"),
        (pr_json(sha="f" * 40), "sha_changed"),
        ({}, "closed"),  # an unexpected body never counts as valid
    ],
)
def test_pr_state(pr: dict[str, Any], code: str) -> None:
    assert pv.pr_state(pr, SHA)[0] == code


def test_pull_request_is_fetched_from_the_documented_path() -> None:
    script = Script(pr_json())
    assert pv.GitHubApi("tok-123456", REPO, script).pull(12)["state"] == "open"
    assert script.requests[0].full_url == f"https://api.github.com/repos/{REPO}/pulls/12"
    with pytest.raises(lib.DeployError):
        pv.GitHubApi("tok-123456", REPO, Script("[]")).pull(12)


def test_comment_is_sanitised_and_has_the_marker() -> None:
    state = {
        "status": "failed",
        "headline": "bad `<b>@everyone</b>`\n## injected",
        "detail": "see [x](http://evil) | a @octocat\nsecond line",
        "digest": DIGEST,
    }
    body = pv.render_comment(
        state, pr_number=12, head_sha=SHA, run_url="https://github.com/o/r/actions/runs/1"
    )
    assert body.startswith(pv.COMMENT_MARKER)
    for forbidden in ("<b>", "@everyone", "@octocat", "](http", "\n## injected"):
        assert forbidden not in body.split(pv.COMMENT_MARKER, 1)[1]
    assert DIGEST in body


def test_comment_withholds_a_bad_url_and_shows_a_good_one() -> None:
    good = {
        "status": "deployed",
        "headline": "DEPLOYED and verified",
        "detail": "",
        "url": "https://a1.onrender.com",
    }
    assert "https://a1.onrender.com" in pv.render_comment(
        good, pr_number=1, head_sha=SHA, run_url=""
    )
    bad = {**good, "url": "https://evil.example/phish"}
    body = pv.render_comment(bad, pr_number=1, head_sha=SHA, run_url="")
    assert "evil.example" not in body
    assert "withheld" in body


def test_cleanup_failure_names_the_service_to_delete_by_hand() -> None:
    state = pv.derive_state(needs(cleanup=("failure", {})), "cleanup")
    body = pv.render_comment(state, pr_number=12, head_sha=SHA, run_url="")
    assert "stockflow-pr-12" in body


# --------------------------------------------------------------------------- CLI
@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in [
        *event(),
        "RENDER_API_KEY",
        "RENDER_SERVICE_ID",
        "RENDER_KEY_PRESENT",
        "RENDER_SERVICE_PRESENT",
        "GITHUB_TOKEN",
        "NEEDS_JSON",
        "PREVIEW_PLAN",
    ]:
        monkeypatch.delenv(name, raising=False)
    for key, value in event().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "sum"))
    return tmp_path


def test_cli_decide_writes_outputs(cli_env: Path) -> None:
    assert preview_tools.main(["decide"]) == 0
    text = (cli_env / "out").read_text()
    assert "action=deploy" in text
    assert "pr_number=12" in text


def test_cli_deploy_and_cleanup_are_refused_for_the_wrong_event(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RENDER_API_KEY", "KEY-123456")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-base1")
    monkeypatch.setenv("PR_HEAD_REPO", "evil/fork")  # a fork: the guard in the script must refuse
    assert preview_tools.main(["deploy", "--image", IMAGE]) == 1
    assert preview_tools.main(["cleanup"]) == 1
    monkeypatch.setenv("PR_HEAD_REPO", REPO)
    assert preview_tools.main(["cleanup"]) == 1  # a 'labeled' event is not a cleanup


def test_cli_preflight_unconfigured_fails_loudly_and_names_what_is_missing(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert preview_tools.main(["preflight"]) == 1
    out = capsys.readouterr().out
    assert "NOT DEPLOYED: preview not configured" in out
    assert "RENDER_API_KEY" in out
    assert "RENDER_SERVICE_ID" in out
    assert "configured=false" in (cli_env / "out").read_text()
    assert "NOT DEPLOYED" in (cli_env / "sum").read_text()


def test_cli_preflight_configured_and_optional(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        preview_tools.main(["preflight", "--optional"]) == 0
    )  # cleanup without config: warn, succeed
    monkeypatch.setenv("RENDER_KEY_PRESENT", "true")
    monkeypatch.setenv("RENDER_SERVICE_PRESENT", "true")
    assert preview_tools.main(["preflight"]) == 0


def test_cli_cleanup_tolerates_a_missing_preview(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in event(EVENT_ACTION="closed", EVENT_LABEL="").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RENDER_API_KEY", "KEY-123456")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-base1")
    monkeypatch.setattr(pv.PreviewApi, "find", lambda self, name: None)
    assert preview_tools.main(["cleanup"]) == 0
    assert "deleted=false" in (cli_env / "out").read_text()


def test_cli_cleanup_deletes_and_reports_failure(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in event(EVENT_ACTION="closed", EVENT_LABEL="").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RENDER_API_KEY", "KEY-123456")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-base1")
    monkeypatch.setattr(
        pv.PreviewApi, "find", lambda self, name: pv.RenderService("srv-mine1", name, None)
    )
    monkeypatch.setattr(pv.PreviewApi, "delete", lambda self, sid: True)
    assert preview_tools.main(["cleanup"]) == 0
    assert "deleted=true" in (cli_env / "out").read_text()

    def boom(self: pv.PreviewApi, sid: str) -> bool:
        raise lib.DeployError("Render API DELETE /services/x answered HTTP 500")

    monkeypatch.setattr(pv.PreviewApi, "delete", boom)
    assert preview_tools.main(["cleanup"]) == 1


def test_cli_deploy_success_writes_outputs(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_API_KEY", "KEY-123456")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-base1")
    result = pv.PreviewResult("srv-new1", "dep-aaa", "https://sf.onrender.com", True)
    monkeypatch.setattr(pv, "deploy_preview", lambda api, **kw: result)
    out = cli_env / "res.json"
    assert preview_tools.main(["deploy", "--image", IMAGE, "--result-file", str(out)]) == 0
    text = (cli_env / "out").read_text()
    assert "url=https://sf.onrender.com" in text
    assert json.loads(out.read_text())["service_id"] == "srv-new1"


def test_cli_deploy_failure_does_not_print_the_key(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RENDER_API_KEY", "KEY-123456")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-base1")

    def boom(api: pv.PreviewApi, **kw: Any) -> pv.PreviewResult:
        raise lib.DeployError("preview deploy did not go live: update_failed")

    monkeypatch.setattr(pv, "deploy_preview", boom)
    assert (
        preview_tools.main(["deploy", "--image", IMAGE, "--result-file", str(cli_env / "r.json")])
        == 1
    )
    captured = capsys.readouterr()
    shown = "\n".join(
        line
        for line in (captured.out + captured.err).splitlines()
        if not line.startswith("::add-mask::")
    )
    assert "KEY-123456" not in shown
    assert "update_failed" in shown


def test_cli_wait_gate(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123456")
    monkeypatch.setattr(
        pv.GitHubApi, "check_runs", lambda self, sha: [run("quality-gate", "completed", "success")]
    )
    assert preview_tools.main(["wait-gate", "--timeout", "1", "--interval", "1"]) == 0
    assert "gate=success" in (cli_env / "out").read_text()
    monkeypatch.setattr(
        pv.GitHubApi, "check_runs", lambda self, sha: [run("quality-gate", "completed", "failure")]
    )
    assert preview_tools.main(["wait-gate", "--timeout", "1", "--interval", "1"]) == 1


def test_cli_comment_posts_the_sticky_comment(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123456")
    monkeypatch.setenv("NEEDS_JSON", json.dumps(needs(**GREEN)))
    posted: list[tuple[int, str]] = []
    monkeypatch.setattr(
        pv.GitHubApi, "upsert_comment", lambda self, n, body: posted.append((n, body)) or "created"
    )
    assert preview_tools.main(["comment"]) == 0
    assert posted[0][0] == 12
    assert "DEPLOYED and verified" in posted[0][1]
    assert "DEPLOYED and verified" in (cli_env / "sum").read_text()


def test_cli_comment_skips_ineligible_events(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_HEAD_REPO", "evil/fork")
    monkeypatch.setattr(
        pv.GitHubApi, "upsert_comment", lambda self, n, body: pytest.fail("must not comment")
    )
    assert preview_tools.main(["comment"]) == 0


# ---- recheck (before / after) and the race it closes
def fake_pull(monkeypatch: pytest.MonkeyPatch, pr: dict[str, Any]) -> None:
    monkeypatch.setattr(pv.GitHubApi, "pull", lambda self, n: pr)


def render_env(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123456")
    monkeypatch.setenv("RENDER_API_KEY", "KEY-123456")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-base1")
    deleted: list[str] = []
    monkeypatch.setattr(
        pv.PreviewApi, "find", lambda self, name: pv.RenderService("srv-mine1", name, None)
    )
    monkeypatch.setattr(pv.PreviewApi, "delete", lambda self, sid: deleted.append(sid) or True)
    return deleted


@pytest.mark.parametrize("phase", ["before", "after"])
def test_recheck_valid_changes_nothing(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    deleted = render_env(monkeypatch)
    fake_pull(monkeypatch, pr_json())
    assert preview_tools.main(["recheck", "--phase", phase]) == 0
    assert "valid=true" in (cli_env / "out").read_text()
    assert deleted == []


def test_recheck_before_on_a_closed_pr_skips_and_deletes_nothing(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted = render_env(monkeypatch)
    fake_pull(monkeypatch, pr_json(state="closed"))
    assert preview_tools.main(["recheck", "--phase", "before"]) == 0
    text = (cli_env / "out").read_text()
    assert "valid=false" in text
    assert "code=closed" in text
    assert deleted == []  # nothing was created yet
    assert "superseded" in (cli_env / "sum").read_text().lower()


@pytest.mark.parametrize("pr", [pr_json(state="closed"), pr_json(labels=())])
def test_recheck_after_deletes_the_preview_when_the_pr_went_away(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, pr: dict[str, Any]
) -> None:
    deleted = render_env(monkeypatch)
    fake_pull(monkeypatch, pr)
    assert preview_tools.main(["recheck", "--phase", "after"]) == 0
    text = (cli_env / "out").read_text()
    assert "valid=false" in text
    assert "deleted=true" in text
    assert deleted == ["srv-mine1"]
    assert "was deleted" in (cli_env / "sum").read_text()


def test_recheck_after_with_a_new_commit_keeps_the_preview(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted = render_env(monkeypatch)
    fake_pull(monkeypatch, pr_json(sha="f" * 40))
    assert preview_tools.main(["recheck", "--phase", "after"]) == 0
    text = (cli_env / "out").read_text()
    assert "code=sha_changed" in text
    assert deleted == []  # the newer run owns the preview


def test_recheck_fails_loudly_when_github_cannot_be_asked(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123456")

    def boom(self: pv.GitHubApi, n: int) -> dict[str, Any]:
        raise lib.DeployError("GitHub API GET /repos/x/pulls/12 answered HTTP 500")

    monkeypatch.setattr(pv.GitHubApi, "pull", boom)
    assert preview_tools.main(["recheck", "--phase", "before"]) == 1


def test_recheck_is_refused_for_a_fork(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_HEAD_REPO", "evil/fork")
    fake_pull(monkeypatch, pr_json())
    assert preview_tools.main(["recheck", "--phase", "before"]) == 1


def test_closed_runs_cleanup_even_after_the_label_was_removed(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sequence labeled -> unlabeled -> synchronize -> closed must not orphan a preview."""
    for key, value in event(EVENT_ACTION="closed", EVENT_LABEL="", PR_LABELS="[]").items():
        monkeypatch.setenv(key, value)
    deleted = render_env(monkeypatch)
    assert preview_tools.main(["decide"]) == 0
    assert "action=cleanup" in (cli_env / "out").read_text()
    assert preview_tools.main(["cleanup"]) == 0
    assert deleted == ["srv-mine1"]


def test_synchronize_after_unlabel_is_a_noop_and_cannot_cancel_cleanup_by_design() -> None:
    assert (
        pv.decide(event(EVENT_ACTION="synchronize", EVENT_LABEL="", PR_LABELS="[]")).action
        == "skip"
    )
    assert pv.decide(event(EVENT_ACTION="unlabeled", PR_LABELS="[]")).action == "cleanup"


def test_cli_comment_stays_quiet_when_there_is_nothing_to_delete(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in event(EVENT_ACTION="closed", EVENT_LABEL="", PR_LABELS="[]").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123456")
    monkeypatch.setenv("NEEDS_JSON", json.dumps(needs(cleanup=("success", {"deleted": "false"}))))
    monkeypatch.setattr(pv.GitHubApi, "find_comment", lambda self, n: None)
    monkeypatch.setattr(
        pv.GitHubApi, "upsert_comment", lambda self, n, b: pytest.fail("no comment expected")
    )
    assert preview_tools.main(["comment"]) == 0
    # ...but an existing sticky comment is updated so it never claims a deleted preview is running
    posted: list[str] = []
    monkeypatch.setattr(pv.GitHubApi, "find_comment", lambda self, n: 5)
    monkeypatch.setattr(
        pv.GitHubApi, "upsert_comment", lambda self, n, b: posted.append(b) or "updated"
    )
    assert preview_tools.main(["comment"]) == 0
    assert "No preview was running" in posted[0]
