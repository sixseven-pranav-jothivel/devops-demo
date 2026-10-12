"""Unit tests for scripts/deploy/deploy_lib.py (no network: HTTP openers are fakes)."""

from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import deploy_lib as lib
import pytest

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
IMAGE = f"ghcr.io/octo-org/stockflow@{DIGEST}"
HOOK = "https://api.render.com/deploy/srv-abc123?key=SECRETKEY99"
SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeResponse:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# --------------------------------------------------------------------------- image references
@pytest.mark.parametrize(
    "ref",
    [
        IMAGE,
        f"ghcr.io/a/stockflow@{DIGEST}",
        f"ghcr.io/my.org_x-y/stockflow@{DIGEST}",
    ],
)
def test_valid_image_refs(ref: str) -> None:
    owner, digest = lib.validate_image_ref(ref)
    assert digest == DIGEST
    assert owner == ref.split("/")[1]


@pytest.mark.parametrize(
    "ref",
    [
        "ghcr.io/octo-org/stockflow:latest",  # mutable tag
        "ghcr.io/octo-org/stockflow:sha-" + SHA,
        f"docker.io/octo-org/stockflow@{DIGEST}",  # wrong registry
        f"ghcr.io/octo-org/other@{DIGEST}",  # wrong repository
        f"ghcr.io/Octo-Org/stockflow@{DIGEST}",  # uppercase owner
        f"ghcr.io/octo-org/stockflow@sha256:{'AB' * 32}",  # uppercase hex
        f"ghcr.io/octo-org/stockflow@sha256:{'ab' * 31}",  # short digest
        f"ghcr.io/octo-org/stockflow@{DIGEST}\nextra",  # newline injection
        f"ghcr.io/octo-org/stockflow@{DIGEST}?x=1",
        f"https://ghcr.io/octo-org/stockflow@{DIGEST}",
        f"ghcr.io/octo org/stockflow@{DIGEST}",
        "",
    ],
)
def test_invalid_image_refs_rejected(ref: str) -> None:
    with pytest.raises(lib.DeployError):
        lib.validate_image_ref(ref)


def test_expected_owner_is_enforced_case_insensitively() -> None:
    assert lib.validate_image_ref(IMAGE, "Octo-Org")[0] == "octo-org"
    with pytest.raises(lib.DeployError, match="expected owner"):
        lib.validate_image_ref(IMAGE, "someone-else")


def test_build_image_ref_lowercases_owner_and_validates_digest() -> None:
    assert lib.build_image_ref("Octo-Org", DIGEST) == IMAGE
    with pytest.raises(lib.DeployError):
        lib.build_image_ref("octo-org", "sha256:abc")
    with pytest.raises(lib.DeployError):
        lib.build_image_ref("bad owner", DIGEST)


@pytest.mark.parametrize("value", [DIGEST, f"  {DIGEST}\n"])
def test_validate_digest_strips_whitespace(value: str) -> None:
    assert lib.validate_digest(value) == DIGEST


@pytest.mark.parametrize("value", ["", "sha256:", "sha1:" + "a" * 40, "abc", f"{DIGEST}0"])
def test_validate_digest_rejects(value: str) -> None:
    with pytest.raises(lib.DeployError):
        lib.validate_digest(value)


def test_parse_push_digest() -> None:
    out = f"latest: digest: {DIGEST} size: 1234\n"
    assert lib.parse_push_digest(out) == DIGEST
    twice = f"a: digest: {DIGEST} size: 1\nb: digest: {DIGEST} size: 1\n"
    assert lib.parse_push_digest(twice) == DIGEST
    with pytest.raises(lib.DeployError, match="could not find"):
        lib.parse_push_digest("nothing here")
    with pytest.raises(lib.DeployError, match="several different"):
        lib.parse_push_digest(f"digest: {DIGEST} size: 1\ndigest: {OTHER} size: 1\n")


# --------------------------------------------------------------------------- hook url and secrets
def test_build_hook_url_url_encodes_image_and_keeps_key() -> None:
    url = lib.build_hook_url(HOOK, IMAGE)
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https"
    assert parts.path == "/deploy/srv-abc123"
    assert "imgURL=ghcr.io%2Focto-org%2Fstockflow%40sha256%3A" in parts.query
    query = urllib.parse.parse_qs(parts.query)
    assert query["key"] == ["SECRETKEY99"]
    assert query["imgURL"] == [IMAGE]


def test_build_hook_url_replaces_existing_img_url() -> None:
    url = lib.build_hook_url(HOOK + "&imgURL=evil.example/x:latest", IMAGE)
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["imgURL"] == [IMAGE]
    assert url.count("imgURL") == 1


@pytest.mark.parametrize(
    "hook", ["http://api.render.com/deploy/srv-x?key=k", "ftp://x/y", "nonsense", ""]
)
def test_build_hook_url_requires_https(hook: str) -> None:
    with pytest.raises(lib.DeployError, match="https"):
        lib.build_hook_url(hook, IMAGE)


def test_redact_removes_secret_and_encoded_form() -> None:
    secret = "https://api.render.com/deploy/srv-abc123?key=SECRETKEY99"
    text = f"failed for {secret} and {urllib.parse.quote(secret, safe='')} ok"
    redacted = lib.redact(text, [secret])
    assert "SECRETKEY99" not in redacted
    assert redacted.count(lib.MASK) == 2


def test_redact_ignores_empty_and_tiny_secrets() -> None:
    assert lib.redact("abc", ["", "a"]) == "abc"


def test_github_mask_commands() -> None:
    assert lib.github_mask_commands(["x", ""]) == ["::add-mask::x"]


# --------------------------------------------------------------------------- trigger_hook
def test_trigger_hook_posts_encoded_url_and_returns_deploy_id() -> None:
    seen: dict[str, Any] = {}

    def opener(request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["timeout"] = timeout
        return FakeResponse(200, json.dumps({"deploy": {"id": "dep-123abc"}}))

    result = lib.trigger_hook(HOOK, IMAGE, opener=opener, timeout=5)
    assert result == lib.HookResult(status=200, deploy_id="dep-123abc", queued=False)
    assert seen["method"] == "POST"
    assert seen["timeout"] == 5
    assert "imgURL=" in seen["url"]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query)["imgURL"] == [IMAGE]


def test_trigger_hook_202_is_queued_without_id() -> None:
    result = lib.trigger_hook(HOOK, IMAGE, opener=lambda r, timeout: FakeResponse(202, ""))
    assert result.queued is True
    assert result.deploy_id is None


@pytest.mark.parametrize("code", [400, 401, 404, 409, 500])
def test_trigger_hook_http_errors_never_leak_the_url(code: int) -> None:
    def opener(request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(request.full_url, code, "boom", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    with pytest.raises(lib.DeployError) as info:
        lib.trigger_hook(HOOK, IMAGE, opener=opener)
    assert f"HTTP {code}" in str(info.value)
    assert "SECRETKEY99" not in str(info.value)
    assert "SECRETKEY99" not in repr(info.value)


def test_trigger_hook_network_error_is_redacted() -> None:
    def opener(request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        raise urllib.error.URLError(f"cannot reach {request.full_url}")

    with pytest.raises(lib.DeployError) as info:
        lib.trigger_hook(HOOK, IMAGE, opener=opener)
    assert "SECRETKEY99" not in str(info.value)
    assert lib.MASK in str(info.value)


def test_trigger_hook_unexpected_status() -> None:
    with pytest.raises(lib.DeployError, match="HTTP 204"):
        lib.trigger_hook(HOOK, IMAGE, opener=lambda r, timeout: FakeResponse(204, ""))


def test_default_opener_refuses_plain_http() -> None:
    request = urllib.request.Request("http://example.invalid/", method="GET")
    with pytest.raises(lib.DeployError, match="non-https"):
        lib.default_opener(request, timeout=1)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"id": "dep-abc"}', "dep-abc"),
        ('{"deploy": {"id": "dep-xyz"}}', "dep-xyz"),
        ('{"deploy": "nope"}', None),
        ('{"id": "bad id!"}', None),
        ("not json", None),
        ("[]", None),
        ("", None),
    ],
)
def test_parse_deploy_id(body: str, expected: str | None) -> None:
    assert lib.parse_deploy_id(body) == expected


# --------------------------------------------------------------------------- render api
def deploys_payload(*items: tuple[str, str, str | None]) -> str:
    return json.dumps(
        [
            {
                "deploy": {
                    "id": i,
                    "status": s,
                    "image": {"ref": f"ghcr.io/octo-org/stockflow@{d}", "sha": d} if d else None,
                },
                "cursor": "c",
            }
            for i, s, d in items
        ]
    )


class ScriptedApi:
    """Opener that answers the Render API from a script of per-call payloads."""

    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        self.requests.append(request)
        body = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return FakeResponse(200, body)


def test_render_api_sends_bearer_to_documented_endpoint() -> None:
    opener = ScriptedApi(deploys_payload(("dep-1", "live", DIGEST)))
    api = lib.RenderApi("KEY-123456", "srv-abc", opener)
    assert api.live_image_sha() == DIGEST
    req = opener.requests[0]
    assert req.full_url == "https://api.render.com/v1/services/srv-abc/deploys?limit=20"
    assert req.get_header("Authorization") == "Bearer KEY-123456"


def test_render_api_rejects_bad_service_id_and_deploy_id() -> None:
    with pytest.raises(lib.DeployError):
        lib.RenderApi("k", "../../x")
    api = lib.RenderApi("k", "srv-abc", ScriptedApi("{}"))
    with pytest.raises(lib.DeployError):
        api.get_deploy("a/b")


def test_render_api_http_error_has_no_key() -> None:
    def opener(request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    with pytest.raises(lib.DeployError) as info:
        lib.RenderApi("KEY-123456", "srv-abc", opener).list_deploys()
    assert "KEY-123456" not in str(info.value)


def test_find_deploy_for_digest_skips_known_ids() -> None:
    opener = ScriptedApi(
        deploys_payload(("dep-new", "build_in_progress", DIGEST), ("dep-old", "live", DIGEST))
    )
    api = lib.RenderApi("k", "srv-abc", opener)
    found = api.find_deploy_for_digest(DIGEST, since=["dep-old"])
    assert found is not None
    assert found.deploy_id == "dep-new"
    assert api.find_deploy_for_digest(OTHER) is None


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def run_wait(
    api: lib.RenderApi, deploy_id: str | None, timeout: float = 100.0
) -> lib.RenderOutcome:
    clock = Clock()
    return lib.wait_for_render_deploy(
        api,
        digest=DIGEST,
        deploy_id=deploy_id,
        known_ids=[],
        timeout=timeout,
        interval=10,
        sleep=clock.sleep,
        now=clock.now,
        log=lambda _m: None,
    )


def single(deploy_id: str, status: str) -> str:
    return json.dumps({"id": deploy_id, "status": status, "image": {"ref": IMAGE, "sha": DIGEST}})


def test_wait_for_render_deploy_live() -> None:
    opener = ScriptedApi(
        single("dep-1", "build_in_progress"),
        single("dep-1", "update_in_progress"),
        single("dep-1", "live"),
    )
    outcome = run_wait(lib.RenderApi("k", "srv-abc", opener), "dep-1")
    assert outcome == lib.RenderOutcome("dep-1", "live", True)
    assert len(opener.requests) == 3


@pytest.mark.parametrize(
    "status", ["build_failed", "update_failed", "canceled", "pre_deploy_failed", "deactivated"]
)
def test_wait_for_render_deploy_failure_statuses(status: str) -> None:
    outcome = run_wait(lib.RenderApi("k", "srv-abc", ScriptedApi(single("dep-1", status))), "dep-1")
    assert outcome.ok is False
    assert outcome.status == status


def test_wait_for_render_deploy_times_out() -> None:
    outcome = run_wait(
        lib.RenderApi("k", "srv-abc", ScriptedApi(single("dep-1", "created"))), "dep-1", timeout=30
    )
    assert outcome == lib.RenderOutcome("dep-1", "timeout", False)


def test_wait_without_id_discovers_deploy_by_digest() -> None:
    opener = ScriptedApi(
        deploys_payload(("dep-0", "live", OTHER)),
        deploys_payload(("dep-9", "update_in_progress", DIGEST), ("dep-0", "live", OTHER)),
        single("dep-9", "live"),
    )
    outcome = run_wait(lib.RenderApi("k", "srv-abc", opener), None)
    assert outcome.ok is True
    assert outcome.deploy_id == "dep-9"


def test_wait_never_found_reports_not_found() -> None:
    api = lib.RenderApi("k", "srv-abc", ScriptedApi("[]"))
    assert run_wait(api, None, timeout=20) == lib.RenderOutcome(None, "not_found", False)


def test_wait_survives_transient_api_errors() -> None:
    calls = {"n": 0}

    def opener(request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "x", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        return FakeResponse(200, single("dep-1", "live"))

    assert run_wait(lib.RenderApi("k", "srv-abc", opener), "dep-1").ok is True


# --------------------------------------------------------------------------- quality gate
def needs(**results: str) -> dict[str, Any]:
    return {name: {"result": result, "outputs": {}} for name, result in results.items()}


def test_gate_passes_only_when_all_success() -> None:
    ok, rows = lib.evaluate_needs(needs(a="success", b="success"), ["a", "b"])
    assert ok is True
    assert rows == [("a", "success"), ("b", "success")]


@pytest.mark.parametrize("bad", ["failure", "skipped", "cancelled"])
def test_gate_fails_on_any_non_success(bad: str) -> None:
    ok, _ = lib.evaluate_needs(needs(a="success", b=bad), ["a", "b"])
    assert ok is False


def test_gate_fails_when_a_required_job_is_missing() -> None:
    ok, rows = lib.evaluate_needs(needs(a="success"), ["a", "b"])
    assert ok is False
    assert ("b", "missing") in rows


def test_gate_fails_on_empty_needs_and_malformed_entries() -> None:
    assert lib.evaluate_needs({})[0] is False
    ok, rows = lib.evaluate_needs({"a": "success"})
    assert ok is False
    assert rows == [("a", "missing")]
    assert lib.evaluate_needs({"a": {"result": None}})[0] is False


# --------------------------------------------------------------------------- preflight
def test_preflight_configured() -> None:
    env = {
        "HOOK_CONFIGURED": "true",
        "STOCKFLOW_BASE_URL": "https://stockflow.example/",
        "RENDER_API_CONFIGURED": "false",
    }
    result = lib.preflight(env)
    assert result["configured"] is True
    assert result["base_url"] == "https://stockflow.example"
    assert result["render_api"] is False


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"HOOK_CONFIGURED": "false", "STOCKFLOW_BASE_URL": "https://x.example"},
        {"HOOK_CONFIGURED": "true", "STOCKFLOW_BASE_URL": ""},
        {"HOOK_CONFIGURED": "true", "STOCKFLOW_BASE_URL": "stockflow.example"},
    ],
)
def test_preflight_not_configured(env: dict[str, str]) -> None:
    result = lib.preflight(env)
    assert result["configured"] is False
    assert result["missing"]


# --------------------------------------------------------------------------- records
def test_classify_deployment_matrix() -> None:
    assert lib.classify_deployment(
        configured=False, deploy_outcome="skipped", verify_outcome="skipped"
    ) == (
        "not_deployed_unconfigured",
        False,
        False,
    )
    assert lib.classify_deployment(
        configured=True, deploy_outcome="failure", verify_outcome="skipped"
    ) == ("failed_deploy", False, False)
    assert lib.classify_deployment(
        configured=True, deploy_outcome="success", verify_outcome="failure"
    ) == ("deployed_unverified", True, False)
    assert lib.classify_deployment(
        configured=True, deploy_outcome="success", verify_outcome="success"
    ) == ("deployed_verified", True, True)
    # an unconfigured run can never be reported as deployed, whatever the step outcomes claim
    assert (
        lib.classify_deployment(
            configured=False, deploy_outcome="success", verify_outcome="success"
        )[1]
        is False
    )


ENV = {
    "GITHUB_REPOSITORY": "octo-org/stockflow-demo",
    "GITHUB_RUN_ID": "42",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_ACTOR": "octocat",
    "GITHUB_WORKFLOW": "release",
    "GITHUB_REF": "refs/heads/main",
}


def make_record(
    status: str = "deployed_verified", deployed: bool = True, verified: bool = True
) -> dict[str, Any]:
    return lib.build_record(
        ENV,
        kind="release",
        image=IMAGE,
        version="0.1.0",
        git_sha=SHA,
        tags=["sha-" + SHA, "0.1.0"],
        status=status,
        deployed=deployed,
        verified=verified,
        base_url="https://stockflow.example",
        deploy_result={
            "render_deploy_id": "dep-1",
            "render_status": "live",
            "previous_image_sha": OTHER,
            "hook_http_status": 200,
        },
        reason="because",
        now=dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC),
    )


def test_build_record_contents() -> None:
    record = make_record()
    assert record["digest"] == DIGEST
    assert record["owner"] == "octo-org"
    assert record["timestamp"] == "2026-10-10T12:00:00Z"
    assert (
        record["workflow"]["run_url"]
        == "https://github.com/octo-org/stockflow-demo/actions/runs/42"
    )
    assert record["deployment"]["previous_image_sha"] == OTHER
    assert json.loads(json.dumps(record)) == record


def test_build_record_rejects_tag_reference() -> None:
    with pytest.raises(lib.DeployError):
        lib.build_record(
            ENV,
            kind="release",
            image="ghcr.io/o/stockflow:latest",
            version="1",
            git_sha=SHA,
            tags=[],
            status="x",
            deployed=False,
            verified=False,
            base_url="",
        )


def test_summary_is_explicit_when_nothing_was_deployed() -> None:
    summary = lib.render_summary(make_record("not_deployed_unconfigured", False, False))
    assert "NOT DEPLOYED" in summary
    assert "| deployed | **false** |" in summary
    assert "nothing was deployed" in summary


def test_summary_for_verified_deploy() -> None:
    summary = lib.render_summary(make_record())
    assert "DEPLOYED and verified" in summary
    assert "| deployed | **true** |" in summary
    assert "NOT DEPLOYED" not in summary


def test_summary_failure_headlines() -> None:
    assert "FAILED" in lib.render_summary(make_record("failed_deploy", False, False))
    assert "VERIFICATION FAILED" in lib.render_summary(
        make_record("deployed_unverified", True, False)
    )


# --------------------------------------------------------------------------- image labels
LABELS = {
    "org.opencontainers.image.version": "0.1.0",
    "org.opencontainers.image.revision": SHA,
}


def test_image_labels_single_platform_shapes() -> None:
    assert lib.image_labels({"config": {"Labels": LABELS}}) == LABELS
    assert lib.image_labels({"Config": {"Labels": LABELS}}) == LABELS
    assert lib.image_labels({"config": {}}) == {}


def test_image_labels_multi_platform_shape() -> None:
    assert (
        lib.image_labels({"linux/arm64": {}, "linux/amd64": {"config": {"Labels": LABELS}}})
        == LABELS
    )


def test_image_labels_rejects_garbage() -> None:
    with pytest.raises(lib.DeployError):
        lib.image_labels(["nope"])


def test_release_identity() -> None:
    assert lib.release_identity(LABELS) == ("0.1.0", SHA)
    with pytest.raises(lib.DeployError):
        lib.release_identity({})
    with pytest.raises(lib.DeployError):
        lib.release_identity({**LABELS, "org.opencontainers.image.revision": "unknown"})


# ---- publish record / observe
def test_build_publish_record() -> None:
    record = lib.build_publish_record(
        ENV,
        image=IMAGE,
        version="0.1.0",
        git_sha=SHA,
        tags=["sha-" + SHA, "0.1.0"],
        build_time="2026-10-10T11:00:00Z",
        now=dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC),
    )
    assert record["digest"] == DIGEST
    assert record["kind"] == "release-publish"
    assert DIGEST in record["rollback_hint"]
    with pytest.raises(lib.DeployError):
        lib.build_publish_record(
            ENV, image=IMAGE, version="1", git_sha="abc", tags=[], build_time=""
        )


def test_fetch_version_parses_and_tolerates_failure() -> None:
    body = json.dumps({"git_sha": SHA, "version": "0.1.0"})
    assert lib.fetch_version(
        "https://x.example/", opener=lambda r, timeout: FakeResponse(200, body)
    ) == {
        "git_sha": SHA,
        "version": "0.1.0",
    }

    def down(request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("refused")

    assert lib.fetch_version("https://x.example", opener=down) is None
    assert (
        lib.fetch_version("https://x.example", opener=lambda r, timeout: FakeResponse(200, "[1]"))
        is None
    )
    with pytest.raises(lib.DeployError):
        lib.fetch_version("ftp://x.example")


def test_hook_secrets_include_the_bare_key() -> None:
    secrets = lib.hook_secrets(HOOK, IMAGE)
    assert HOOK in secrets
    assert "SECRETKEY99" in secrets
    assert any("imgURL=" in s for s in secrets)


# ---- summary sanitising (user-supplied reason, labels) -------------------------------------------
@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("line one\nline two", "\n"),
        ("evil\r\n| injected | row |", "|"),
        ("a `code` b", "`"),
        ("<img src=x onerror=1>", "<"),
        ("<script>", ">"),
        ("[click](http://evil.example)", "["),
        ("thanks @octocat and @org/team", "@"),
        ("tab\there\x1b[31mred\x00nul", "\x1b"),
        ("back\\slash", "\\"),
        ("line sep" + chr(0x2028) + "here", chr(0x2028)),
    ],
)
def test_md_safe_strips_markup(raw: str, forbidden: str) -> None:
    assert forbidden not in lib.md_safe(raw)


def test_md_safe_keeps_plain_text_and_caps_length() -> None:
    assert lib.md_safe("rolling back: bad total (see #12)") == "rolling back: bad total (see #12)"
    assert len(lib.md_safe("x" * 1000)) == 200
    assert lib.md_safe("  a \n\n b  ") == "a b"
    assert lib.md_safe(None) == "None"
    assert "(at)octocat" in lib.md_safe("@octocat")


def test_summary_neutralises_a_hostile_reason() -> None:
    record = make_record()
    record["reason"] = "fix\n## Fake heading\n| x | y |\n<b>@everyone</b> `rm -rf` [a](http://evil)"
    summary = lib.render_summary(record)
    reason_lines = [line for line in summary.splitlines() if line.startswith("| reason |")]
    assert len(reason_lines) == 1
    cell = reason_lines[0]
    assert cell.count("|") == 3  # leading, separator, trailing: no injected cells
    for bad in ("<", ">", "`", "[", "@", "\r"):
        assert bad not in cell
    assert "\n## Fake heading" not in summary
    assert not any(line.startswith("## Fake") for line in summary.splitlines())


def test_summary_neutralises_hostile_labels() -> None:
    record = make_record()
    record["version"] = "1.0`\n## pwn"
    record["verification"]["base_url"] = "https://x.example/<script>"
    summary = lib.render_summary(record)
    assert "## pwn" not in "\n".join(line for line in summary.splitlines() if line.startswith("##"))
    assert "<script>" not in summary


# ---- mandatory deployment: preflight flag, end-to-end verification ---
def test_preflight_reports_required_flag() -> None:
    assert lib.preflight({"DEPLOY_REQUIRED": "true"})["required"] is True
    assert lib.preflight({"DEPLOY_REQUIRED": "false"})["required"] is False
    assert lib.preflight({})["required"] is False


def test_preflight_missing_names_the_exact_items() -> None:
    missing = " ".join(lib.preflight({})["missing"])
    assert "RENDER_DEPLOY_HOOK_URL" in missing
    assert "STOCKFLOW_BASE_URL" in missing


RELEASE_RECORD = {"digest": DIGEST, "image": IMAGE, "git_sha": SHA, "version": "0.1.0"}
SERVING = {"name": "stockflow", "git_sha": SHA, "version": "0.1.0"}


def verify(**override: Any) -> list[str]:
    args: dict[str, Any] = {
        "release_record": RELEASE_RECORD,
        "sent_image": IMAGE,
        "expect_sha": SHA,
        "expect_version": "0.1.0",
        "observed": SERVING,
    }
    args.update(override)
    return lib.verify_deployment(**args)


def test_verify_deployment_passes_when_everything_agrees() -> None:
    assert verify() == []


def test_verify_deployment_detects_digest_mismatch() -> None:
    other_image = f"ghcr.io/octo-org/stockflow@{OTHER}"
    problems = verify(sent_image=other_image)
    assert any("digest mismatch" in p for p in problems)
    assert any("differs from release-record" in p for p in problems)


def test_verify_deployment_detects_wrong_serving_sha_and_version() -> None:
    problems = verify(observed={**SERVING, "git_sha": "f" * 40, "version": "0.0.9"})
    assert any("deployed git_sha" in p for p in problems)
    assert any("deployed version" in p for p in problems)


def test_verify_deployment_detects_record_for_another_commit_or_version() -> None:
    assert any("not" in p for p in verify(release_record={**RELEASE_RECORD, "git_sha": "e" * 40}))
    assert any("version" in p for p in verify(release_record={**RELEASE_RECORD, "version": "9"}))


def test_verify_deployment_unreachable_service_and_wrong_name() -> None:
    assert any("did not answer" in p for p in verify(observed=None))
    assert any("name" in p for p in verify(observed={**SERVING, "name": "other"}))


def test_verify_deployment_rejects_a_tag_reference() -> None:
    with pytest.raises(lib.DeployError):
        verify(sent_image="ghcr.io/octo-org/stockflow:latest")


def test_wait_fails_when_render_deploy_is_for_a_different_image() -> None:
    other = json.dumps(
        {
            "id": "dep-1",
            "status": "live",
            "image": {"ref": f"ghcr.io/o/stockflow@{OTHER}", "sha": OTHER},
        }
    )
    outcome = run_wait(lib.RenderApi("k", "srv-abc", ScriptedApi(other)), "dep-1")
    assert outcome.ok is False
    assert outcome.status == "image_mismatch"


def test_summary_marks_mandatory_failure() -> None:
    record = make_record("not_deployed_unconfigured", False, False)
    record["deploy_required"] = True
    summary = lib.render_summary(record)
    assert "mandatory" in summary
    assert "FAILED" in summary
    assert "nothing was deployed" in summary
    assert "workflow_dispatch" in summary


def test_record_marks_deployed_true_only_when_verified() -> None:
    for deploy, verified, expected in [
        ("success", "success", True),
        ("success", "failure", False),
        ("success", "skipped", False),
        ("failure", "skipped", False),
    ]:
        status, deployed, ok = lib.classify_deployment(
            configured=True, deploy_outcome=deploy, verify_outcome=verified
        )
        assert (deployed and ok) is expected, (deploy, verified, status)
