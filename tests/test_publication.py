import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import socket
import threading

import pytest

from proxyhub import publication
from proxyhub.publication import PublicationError, build_bundle, save_bundle, validate_bundle
from proxyhub.publisher import PublisherHandler, PublisherServer, load_snapshot


@pytest.fixture
def profile():
    return {
        "id": "primary", "host": "sub.example.test", "token": "a" * 43, "name": "我的订阅 & One",
        "yaml": "generation: one\n", "raw": base64.b64encode(b"vless://fixture-one\n").decode(),
        "userinfo": {"upload": 10, "download": 20, "total": 300, "expire": 2000000000},
        "update_interval_hours": 12, "reset_day": 15, "reset_at": 1900000000,
    }


@pytest.fixture
def live_server(profile, tmp_path):
    path = tmp_path / "bundle.json"
    save_bundle(path, build_bundle([profile], "one"))
    server = PublisherServer(("127.0.0.1", 0), path)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, path, method="GET", host="sub.example.test", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(method, path, headers={"Host": host, **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_metadata_is_per_profile_and_reset_does_not_replace_expiry(profile):
    other = copy.deepcopy(profile)
    other.update(id="second", token="b" * 43, name="Second", yaml="second: true\n")
    other["userinfo"]["download"] = 999
    bundle = build_bundle([profile, other], "generation-1")
    first, second = bundle["profiles"]
    assert first["responses"]["yaml"]["body"] != second["responses"]["yaml"]["body"]
    assert first["responses"]["yaml"]["headers"]["Subscription-Userinfo"] != second["responses"]["yaml"]["headers"]["Subscription-Userinfo"]
    for response in first["responses"].values():
        headers = response["headers"]
        assert headers["Subscription-Userinfo"] == "upload=10; download=20; total=300; expire=2000000000"
        assert "reset" not in str(headers).lower()
        assert headers["Profile-Update-Interval"] == "12"
        assert "filename*=UTF-8''%E6%88%91" in headers["Content-Disposition"]
    assert first["management"]["reset_at"] == 1900000000
    assert first["management"]["traffic_source"] == "manual"
    profile["userinfo"]["download"] = 12345
    assert first["management"]["userinfo"]["download"] == 20


@pytest.mark.parametrize(("userinfo", "expected"), [
    ({}, None),
    ({"expire": 2000000000}, "expire=2000000000"),
    ({"expire": 0}, "expire=0"),
    ({"upload": 0, "download": 0, "total": 0}, "upload=0; download=0; total=0"),
])
def test_unknown_traffic_and_independent_expiry_are_preserved(profile, userinfo, expected):
    profile["userinfo"] = userinfo
    bundle = build_bundle([profile], "one")
    for response in bundle["profiles"][0]["responses"].values():
        assert response["headers"].get("Subscription-Userinfo") == expected
    assert bundle["profiles"][0]["management"]["userinfo"] == userinfo
    assert validate_bundle(bundle) == bundle


@pytest.mark.parametrize("userinfo", [
    {"upload": 0}, {"download": 0, "total": 0}, {"expire": 1, "total": 0},
    {"expire": None}, {"unknown": 0},
])
def test_incomplete_or_invalid_traffic_metadata_is_rejected(profile, userinfo):
    profile["userinfo"] = userinfo
    with pytest.raises(PublicationError):
        build_bundle([profile], "one")


@pytest.mark.parametrize(("field", "value"), [
    ("token", "short"), ("token", "../" + "x" * 40), ("host", "sub.example.test\r\nX: bad"),
    ("host", "Sub.example.test"), ("host", "sub.example.test:65536"), ("name", "../name"),
    ("update_interval_hours", 0), ("update_interval_hours", True), ("reset_day", 32),
    ("raw", "not-base64"), ("unknown", 1),
])
def test_invalid_profile_rejected(profile, field, value):
    profile[field] = value
    with pytest.raises(PublicationError):
        build_bundle([profile], "one")


def test_traffic_fields_and_duplicate_routes_rejected(profile):
    profile["userinfo"]["expire"] = -1
    with pytest.raises(PublicationError):
        build_bundle([profile], "one")
    profile["userinfo"]["expire"] = 0
    other = copy.deepcopy(profile)
    other["id"] = "second"
    with pytest.raises(PublicationError):
        build_bundle([profile, other], "one")


def test_tampered_managed_headers_rejected(profile):
    bundle = build_bundle([profile], "one")
    bundle["profiles"][0]["responses"]["yaml"]["headers"]["Subscription-Userinfo"] = "expire=1\r\nInjected: yes"
    with pytest.raises(PublicationError):
        validate_bundle(bundle)


def test_save_failure_preserves_old_file_and_cleans_candidate(profile, tmp_path, monkeypatch):
    path = tmp_path / "bundle.json"
    old = build_bundle([profile], "old")
    save_bundle(path, old)
    previous_bytes = path.read_bytes()
    def fail_replace(*args):
        raise OSError("test replacement failure")
    monkeypatch.setattr(publication.os, "replace", fail_replace)
    with pytest.raises(OSError):
        save_bundle(path, build_bundle([profile], "new"))
    assert path.read_bytes() == previous_bytes
    assert sorted(item.name for item in tmp_path.iterdir()) == ["bundle.json"]


def test_get_head_have_same_metadata_and_exact_content_length(profile, live_server):
    server, _ = live_server
    for kind, suffix in (("yaml", "mihomo.yaml"), ("raw", "raw")):
        path = f"/sub/{profile['token']}/{suffix}"
        status, headers, body = request(server, path)
        head_status, head_headers, head_body = request(server, path, "HEAD")
        assert status == head_status == 200
        assert headers == head_headers
        assert body.decode() == profile[kind] and head_body == b""
        assert int(headers["Content-Length"]) == len(body)
        assert headers["X-ProxyHub-Generation"] == "one"


@pytest.mark.parametrize("suffix", ["?x=1", "?", "#fragment", "/", "/../raw", "%3Fignored"])
def test_query_encoded_and_nonexact_paths_rejected(profile, live_server, suffix):
    server, _ = live_server
    status, _, body = request(server, f"/sub/{profile['token']}/mihomo.yaml{suffix}")
    assert status == 404 and body == b""


def test_wrong_host_token_and_normalized_paths_rejected(profile, live_server):
    server, _ = live_server
    path = f"/sub/{profile['token']}/mihomo.yaml"
    assert request(server, path, host="attacker.example.test")[0] == 404
    assert request(server, path, host="sub.example.test:443")[0] == 404
    assert request(server, path.replace("a" * 43, "b" * 43))[0] == 404
    assert request(server, "/" + path)[0] == 404
    assert request(server, path.replace("/sub/", "/%73ub/"))[0] == 404


def test_duplicate_host_and_oversized_headers_rejected(profile, live_server):
    server, _ = live_server
    path = f"/sub/{profile['token']}/raw"
    for extra, expected in ((b"Host: sub.example.test\r\n", b" 404 "), (b"X-Padding: " + b"x" * 9000 + b"\r\n", b" 431 ")):
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as client:
            client.sendall(f"GET {path} HTTP/1.1\r\nHost: sub.example.test\r\n".encode() + extra + b"\r\n")
            response = client.recv(2048)
        assert expected in response.split(b"\r\n", 1)[0]


def test_only_get_and_head_without_request_bodies(profile, live_server):
    server, _ = live_server
    path = f"/sub/{profile['token']}/raw"
    assert request(server, path, "POST")[0] == 405
    assert request(server, path, "OPTIONS")[0] == 405
    assert request(server, path, headers={"Content-Length": "1"})[0] == 400


def test_reload_failure_retains_entire_snapshot(profile, live_server, capsys):
    server, path = live_server
    old = server.snapshot
    path.write_text('{"corrupt":"sensitive-token"}', encoding="utf-8")
    assert server.reload_bundle() is False
    assert server.snapshot is old
    status, headers, body = request(server, f"/sub/{profile['token']}/mihomo.yaml")
    assert status == 200 and headers["X-ProxyHub-Generation"] == "one"
    assert body.decode() == profile["yaml"]
    assert capsys.readouterr().out == ""


def test_empty_bundle_withdraws_all_routes(profile, live_server):
    server, path = live_server
    save_bundle(path, build_bundle([], "withdrawn"))
    assert server.reload_bundle() is True
    assert request(server, f"/sub/{profile['token']}/mihomo.yaml")[0] == 404
    assert request(server, f"/sub/{profile['token']}/raw")[0] == 404


def test_inflight_request_uses_one_immutable_generation(profile, live_server, monkeypatch):
    server, path = live_server
    captured = threading.Event()
    release = threading.Event()
    original = PublisherHandler.send_header
    def delay_old_header(handler, keyword, value):
        if keyword == "X-ProxyHub-Generation" and value == "one":
            captured.set()
            assert release.wait(5)
        return original(handler, keyword, value)
    monkeypatch.setattr(PublisherHandler, "send_header", delay_old_header)
    route = f"/sub/{profile['token']}/mihomo.yaml"
    with ThreadPoolExecutor(max_workers=1) as pool:
        inflight = pool.submit(request, server, route)
        try:
            assert captured.wait(5)
            changed = copy.deepcopy(profile)
            changed["yaml"] = "generation: two\n"
            changed["userinfo"]["download"] = 222
            save_bundle(path, build_bundle([changed], "two"))
            assert server.reload_bundle() is True
        finally:
            release.set()
        _, headers, body = inflight.result(timeout=5)
    assert headers["X-ProxyHub-Generation"] == "one" and "download=20;" in headers["Subscription-Userinfo"]
    assert body.decode() == profile["yaml"]
    _, new_headers, new_body = request(server, route)
    assert new_headers["X-ProxyHub-Generation"] == "two" and "download=222;" in new_headers["Subscription-Userinfo"]
    assert new_body.decode() == changed["yaml"]


def test_restart_reads_last_valid_bundle_and_snapshot_is_immutable(profile, tmp_path):
    path = tmp_path / "bundle.json"
    save_bundle(path, build_bundle([profile], "persisted"))
    snapshot = load_snapshot(path)
    assert snapshot.generation == "persisted"
    with pytest.raises(TypeError):
        snapshot.routes[("host", "path")] = None
    with pytest.raises(PublicationError):
        PublisherServer(("0.0.0.0", 0), path)


def test_duplicate_json_fields_rejected(profile, tmp_path):
    path = tmp_path / "bundle.json"
    text = json.dumps(build_bundle([profile], "one"))
    path.write_text(text.replace('"version": 1', '"version": 1, "version": 1'), encoding="utf-8")
    with pytest.raises(PublicationError):
        load_snapshot(path)
