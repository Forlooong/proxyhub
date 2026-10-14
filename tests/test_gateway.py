from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from proxyhub.gateway import COOKIE, MAX_REQUEST, PORT, SECURITY_HEADERS, create_app
from proxyhub.rpc import RpcError

ORIGIN = f"http://127.0.0.1:{PORT}"
CREDENTIAL = "test-credential-never-a-real-credential"


@pytest.fixture
def config(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Panel test</title>")
    (assets / "panel.js").write_text("console.log('test')")
    return {"credential_sha256": hashlib.sha256(CREDENTIAL.encode()).hexdigest(),
            "port": PORT, "session_ttl_seconds": 1800, "assets_path": str(assets)}


@pytest.fixture
def client(config):
    calls = []
    def invoke(name, payload):
        calls.append((name, payload))
        return {"revision": 7}
    with TestClient(create_app(config, rpc_call=invoke), base_url=ORIGIN) as value:
        value.calls = calls
        yield value


def login(client):
    result = client.post("/api/login", json={"credential": CREDENTIAL}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return result.json()["csrf"]


def operation(client, csrf, body=None, **kwargs):
    return client.post("/api/operation", json=body or {"operation": "overview", "payload": {}},
                       headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, **kwargs)


def test_complete_session_operation_logout_flow(client):
    assert client.get("/api/session").json() == {"authenticated": False}
    csrf = login(client)
    assert client.get("/api/session").json() == {"authenticated": True, "csrf": csrf}
    assert operation(client, csrf).json() == {"ok": True, "result": {"revision": 7}}
    assert client.calls == [("overview", {})]
    result = client.post("/api/logout", json={}, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert result.json() == {"authenticated": False}
    assert operation(client, csrf).status_code == 401
    assert client.get("/api/session").json() == {"authenticated": False}


def test_cookie_is_not_readable_to_javascript_or_cross_site(client):
    response = client.post("/api/login", json={"credential": CREDENTIAL}, headers={"Origin": ORIGIN})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert CREDENTIAL not in cookie and "credential" not in response.text


@pytest.mark.parametrize("path", ["/", "/api/session", "/missing.js"])
def test_all_responses_have_security_headers_and_no_cors(client, path):
    response = client.get(path)
    for key, expected in SECURITY_HEADERS.items():
        assert response.headers[key] == expected
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "localhost:80", "127.0.0.1:18765.evil", "[::1]:18765"])
def test_exact_host_prevents_dns_rebinding(client, host):
    response = client.get("/api/session", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_host"


def test_duplicate_host_is_rejected(client):
    assert client.get("/api/session", headers=[("Host", "127.0.0.1:18765"), ("Host", "evil")]).status_code == 400


@pytest.mark.parametrize("origin", [None, "null", "https://127.0.0.1:18765", "http://evil.example", "http://localhost:18765"])
def test_login_requires_exact_origin_for_the_selected_host(client, origin):
    headers = {} if origin is None else {"Origin": origin}
    result = client.post("/api/login", json={"credential": CREDENTIAL}, headers=headers)
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "invalid_origin"


def test_authentication_and_csrf_are_both_required_before_rpc(client):
    assert operation(client, "wrong").status_code == 401
    csrf = login(client)
    assert operation(client, "wrong").status_code == 403
    result = client.post("/api/operation", json={"operation": "overview", "payload": {}}, headers={"Origin": ORIGIN})
    assert result.status_code == 403
    assert client.calls == []
    assert operation(client, csrf).status_code == 200


def test_duplicate_csrf_and_cookie_fields_do_not_authenticate(client):
    csrf = login(client)
    body = {"operation": "overview", "payload": {}}
    headers = [("Origin", ORIGIN), ("X-CSRF-Token", csrf), ("X-CSRF-Token", csrf)]
    assert client.post("/api/operation", json=body, headers=headers).status_code == 403
    cookie = client.cookies.get(COOKIE)
    headers = {"Cookie": f"{COOKIE}={cookie}; {COOKIE}={cookie}"}
    assert client.get("/api/session", headers=headers).json() == {"authenticated": False}


def test_login_rotates_the_previous_session(client):
    old_csrf = login(client)
    old_cookie = client.cookies.get(COOKIE)
    new_csrf = login(client)
    assert new_csrf != old_csrf
    assert client.cookies.get(COOKIE) != old_cookie
    assert client.get("/api/session", headers={"Cookie": f"{COOKIE}={old_cookie}"}).json() == {"authenticated": False}


def test_session_collection_has_a_firm_upper_bound(config, monkeypatch):
    now = time.monotonic()
    offset = [0]
    monkeypatch.setattr("proxyhub.gateway.time.monotonic", lambda: now + offset[0])
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        first_cookie = None
        for index in range(129):
            if index and index % 10 == 0:
                offset[0] += 61
            client.cookies.clear()
            login(client)
            if index == 0:
                first_cookie = client.cookies.get(COOKIE)
        # Only 12 minutes elapsed, so the 30-minute TTL has not caused this eviction.
        assert client.get("/api/session", headers={"Cookie": f"{COOKIE}={first_cookie}"}).json() == {"authenticated": False}
        assert client.get("/api/session").json()["authenticated"] is True


def test_session_expiration_uses_monotonic_time(client, monkeypatch):
    login(client)
    now = time.monotonic()
    monkeypatch.setattr("proxyhub.gateway.time.monotonic", lambda: now + 1801)
    assert client.get("/api/session").json() == {"authenticated": False}


def test_login_attempts_are_bounded_and_recover_after_window(client, monkeypatch):
    for _ in range(10):
        assert client.post("/api/login", json={"credential": "wrong"}, headers={"Origin": ORIGIN}).status_code == 401
    response = client.post("/api/login", json={"credential": CREDENTIAL}, headers={"Origin": ORIGIN})
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
    now = time.monotonic()
    monkeypatch.setattr("proxyhub.gateway.time.monotonic", lambda: now + 61)
    assert client.post("/api/login", json={"credential": CREDENTIAL}, headers={"Origin": ORIGIN}).status_code == 200


@pytest.mark.parametrize("body", [
    {"operation": "shell", "payload": {"command": "id"}},
    {"operation": "overview", "payload": {}, "path": "/etc/shadow"},
    {"operation": "overview", "payload": []},
    {"operation": ["overview"], "payload": {}},
])
def test_gateway_rejects_untyped_operations_without_dispatch(client, body):
    result = operation(client, login(client), body)
    assert result.status_code == 400 and not client.calls


@pytest.mark.parametrize("body", [b'{"credential":"a","credential":"b"}', b'{"credential": NaN}', b'\xff', b'{} trailing'])
def test_invalid_json_and_duplicate_fields_are_rejected(client, body):
    result = client.post("/api/login", content=body, headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "invalid_request"


def test_body_limit_checks_actual_streamed_bytes(client):
    response = client.post("/api/login", content=iter([b'{"credential":"', b'a' * MAX_REQUEST, b'"}']),
                           headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 413


def test_body_timeout_before_json_decode(config):
    app = create_app(config, rpc_call=lambda *_: None)
    messages = []
    async def run():
        scope = {"type": "http", "method": "POST", "path": "/api/login", "headers": [
            (b"host", b"127.0.0.1:18765"), (b"origin", ORIGIN.encode()), (b"content-type", b"application/json")],
            "query_string": b"", "http_version": "1.1", "scheme": "http", "server": ("127.0.0.1", PORT), "client": ("127.0.0.1", 12345)}
        async def receive():
            await asyncio.sleep(6)
            return {"type": "http.request", "body": b"{}", "more_body": False}
        async def send(value):
            messages.append(value)
        await app(scope, receive, send)
    asyncio.run(run())
    assert messages[0]["status"] == 408


def test_oversized_headers_are_rejected(client):
    assert client.get("/", headers={"X-Unused": "a" * 8192}).status_code == 431


def test_form_login_cannot_bypass_json_policy(client):
    assert client.post("/api/login", data={"credential": CREDENTIAL}, headers={"Origin": ORIGIN}).status_code == 415


@pytest.mark.parametrize("path", ["/%2e%2e/secrets", "/assets/%2e%2e/index.html", "/..%5csecret", "/api/unknown", "/docs", "/openapi.json"])
def test_static_routes_do_not_expose_paths_or_api_docs(client, path):
    response = client.get(path)
    # /docs is a client-side route, not generated interactive API documentation.
    if path == "/docs":
        assert "swagger" not in response.text.lower()
    else:
        assert response.status_code == 404


def test_static_assets_and_spa_routes(client):
    assert client.get("/panel.js").status_code == 200
    assert client.get("/subscription").text.startswith("<!doctype html>")
    assert client.head("/panel.js").content == b""


def test_symlink_cannot_serve_a_file_outside_assets(config, tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text("never send this")
    try:
        (tmp_path / "assets" / "secret.txt").symlink_to(target)
    except OSError:
        pytest.skip("Platform does not allow symlink creation")
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        assert client.get("/secret.txt").status_code == 404


def test_index_symlink_cannot_escape_the_static_directory(config, tmp_path):
    target = tmp_path / "secret.html"
    target.write_text("private material")
    index = tmp_path / "assets" / "index.html"
    index.unlink()
    try:
        index.symlink_to(target)
    except OSError:
        pytest.skip("Platform does not allow symlink creation")
    with pytest.raises(ValueError, match="assets are missing"):
        create_app(config)


def test_unexpected_rpc_exception_is_redacted(config):
    def fail(*_):
        raise ValueError("private-key=SUPER_SECRET")
    with TestClient(create_app(config, rpc_call=fail), base_url=ORIGIN) as client:
        response = operation(client, login(client))
        assert response.status_code == 500
        assert "SUPER_SECRET" not in response.text


def test_safe_rpc_errors_are_returned(config):
    def fail(*_):
        raise RpcError("revision_conflict", "The deployment changed; create a new preview")
    with TestClient(create_app(config, rpc_call=fail), base_url=ORIGIN) as client:
        result = operation(client, login(client))
        assert result.json()["error"]["code"] == "revision_conflict"


def test_real_http_login_and_dispatch(config):
    import uvicorn
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(config, rpc_call=lambda *_: {"revision": 3}),
        access_log=False, log_level="critical", proxy_headers=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", "/api/login", body='{"credential":"' + CREDENTIAL + '"}',
                           headers={"Host": "127.0.0.1:18765", "Origin": ORIGIN, "Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200 and response.getheader("Cache-Control") == "no-store"
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        logged_in = json.loads(response.read())
        assert logged_in["authenticated"] is True
        connection.request("POST", "/api/operation", body='{"operation":"overview","payload":{}}',
                           headers={"Host": "127.0.0.1:18765", "Origin": ORIGIN, "Content-Type": "application/json",
                                    "Cookie": cookie, "X-CSRF-Token": logged_in["csrf"]})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"ok": True, "result": {"revision": 3}}
        connection.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("replacement", [{"port": 80}, {"session_ttl_seconds": 0}, {"credential_sha256": "abc"}, {"extra": 1}])
def test_invalid_configuration_fails_closed(config, replacement):
    with pytest.raises(ValueError):
        create_app(config | replacement)
