"""Redirects must never carry credentials to another host (Render API key, deploy-hook key).

Real sockets on loopback: server A answers 302 pointing at server B (a different host name, so a
naive client would follow and forward headers); server B records every request it receives.
"""

from __future__ import annotations

import http.server
import ssl
import subprocess
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from shutil import which

import deploy_lib as lib
import pytest

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "ab" * 32
IMAGE = f"ghcr.io/octo-org/stockflow@{DIGEST}"


class Recorder(http.server.BaseHTTPRequestHandler):
    """Target of the redirect: records everything it is sent."""

    received: list[dict[str, str]]

    def _record(self) -> None:
        self.received.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = _record  # noqa: N815

    def log_message(self, *args: object) -> None:
        return None


def redirector(location: str) -> type[http.server.BaseHTTPRequestHandler]:
    class Redirect(http.server.BaseHTTPRequestHandler):
        def _go(self) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        do_GET = do_POST = _go  # noqa: N815

        def log_message(self, *args: object) -> None:
            return None

    return Redirect


def serve(server: http.server.HTTPServer) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


@pytest.fixture
def target() -> Iterator[tuple[http.server.HTTPServer, list[dict[str, str]]]]:
    received: list[dict[str, str]] = []
    handler = type("H", (Recorder,), {"received": received})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    serve(server)
    yield server, received
    server.shutdown()
    server.server_close()


def start_redirect_http(location: str) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", 0), redirector(location))
    serve(server)
    return server


def test_open_no_redirect_refuses_and_never_contacts_the_other_host(
    target: tuple[http.server.HTTPServer, list[dict[str, str]]],
) -> None:
    other, received = target
    a = start_redirect_http(f"http://localhost:{other.server_port}/steal")
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{a.server_port}/v1/x", method="GET")
        request.add_header("Authorization", "Bearer RENDER-SECRET-KEY")
        with pytest.raises(lib.DeployError, match="redirect") as info:
            lib.open_no_redirect(request, timeout=5)
        assert "steal" not in str(info.value)  # the target is not echoed
        assert received == []  # the second host never saw anything, least of all the key
    finally:
        a.shutdown()
        a.server_close()


def test_open_no_redirect_still_returns_normal_responses(
    target: tuple[http.server.HTTPServer, list[dict[str, str]]],
) -> None:
    server, received = target
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/ok", method="GET")
    with lib.open_no_redirect(request, timeout=5) as response:
        assert response.status == 200
    assert received[0]["path"] == "/ok"


def test_fetch_version_treats_a_redirect_as_unreachable(
    target: tuple[http.server.HTTPServer, list[dict[str, str]]],
) -> None:
    other, received = target
    a = start_redirect_http(f"http://localhost:{other.server_port}/elsewhere")
    try:
        assert lib.fetch_version(f"http://127.0.0.1:{a.server_port}") is None
        assert received == []
    finally:
        a.shutdown()
        a.server_close()


# ---- the https path (hook POST and Render API GET) needs a throw-away certificate
@pytest.fixture
def tls_cert(tmp_path: Path) -> Path:
    openssl = which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert


def start_redirect_tls(location: str, cert: Path) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", 0), redirector(location))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, cert.with_name("key.pem"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    serve(server)
    return server


def test_deploy_hook_key_is_never_forwarded_on_a_redirect(
    target: tuple[http.server.HTTPServer, list[dict[str, str]]],
    tls_cert: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other, received = target
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_cert))
    hook_server = start_redirect_tls(
        f"http://localhost:{other.server_port}/hook?key=HOOKKEY99", tls_cert
    )
    try:
        hook = f"https://localhost:{hook_server.server_port}/deploy/srv-x?key=HOOKKEY99"
        with pytest.raises(lib.DeployError) as info:
            lib.trigger_hook(hook, IMAGE)
        assert "HOOKKEY99" not in str(info.value)
        assert received == []
    finally:
        hook_server.shutdown()
        hook_server.server_close()


def test_render_api_key_is_never_forwarded_on_a_redirect(
    target: tuple[http.server.HTTPServer, list[dict[str, str]]],
    tls_cert: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other, received = target
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_cert))
    api_server = start_redirect_tls(f"http://localhost:{other.server_port}/v1/steal", tls_cert)
    try:
        # point the client at the fake host instead of api.render.com
        monkeypatch.setattr(lib, "RENDER_API", f"https://localhost:{api_server.server_port}/v1")
        api = lib.RenderApi("RENDER-SECRET-KEY", "srv-abc123")
        with pytest.raises(lib.DeployError, match="redirect"):
            api.list_deploys()
        assert received == []
        assert not any("authorization" in r for r in received)
    finally:
        api_server.shutdown()
        api_server.server_close()


# ---- base url validation
@pytest.mark.parametrize(
    "url",
    [
        "https://stockflow.onrender.com",
        "https://stockflow.onrender.com/",
        "http://127.0.0.1:8000",
        "http://localhost:18555/",
        "http://[::1]:8000",
    ],
)
def test_validate_base_url_accepts(url: str) -> None:
    assert lib.validate_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://stockflow.onrender.com",  # plain http to a real host
        "http://127.0.0.1.evil.example",  # look-alike host
        "http://localhost.evil.example",
        "ftp://stockflow.example",
        "stockflow.example",
        "https://",
        "",
    ],
)
def test_validate_base_url_rejects(url: str) -> None:
    with pytest.raises(lib.DeployError, match="https"):
        lib.validate_base_url(url)


def test_preflight_requires_https_for_a_real_host() -> None:
    base = {"HOOK_CONFIGURED": "true"}
    bad = lib.preflight({**base, "STOCKFLOW_BASE_URL": "http://stockflow.onrender.com"})
    assert bad["configured"] is False
    assert any("https://" in m and "STOCKFLOW_BASE_URL" in m for m in bad["missing"])
    assert lib.preflight({**base, "STOCKFLOW_BASE_URL": "https://stockflow.onrender.com"})[
        "configured"
    ]
    assert lib.preflight({**base, "STOCKFLOW_BASE_URL": "http://127.0.0.1:8000"})["configured"]


def test_fetch_version_refuses_plain_http_to_a_real_host() -> None:
    with pytest.raises(lib.DeployError, match="https"):
        lib.fetch_version("http://stockflow.onrender.com")
