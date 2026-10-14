from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
import types
import uuid

import pytest

from proxyhub.manager import ManagerServer, peer_uid
from proxyhub.rpc import MAX_REQUEST, MAX_RESPONSE, RpcError, call, decode_json, encode_json, read_frame, validate_request


class SafeFailure(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message


class Service:
    def __init__(self):
        self.calls = []

    def handle(self, operation, payload):
        self.calls.append((operation, payload))
        if payload.get("fail"):
            raise ValueError("SECRET_TEST_CREDENTIAL")
        if payload.get("safe_fail"):
            raise SafeFailure("revision_conflict", "Create a fresh preview")
        if payload.get("oversized"):
            return "a" * MAX_RESPONSE
        return {"revision": 42}


@pytest.fixture
def manager(tmp_path):
    if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("Linux SO_PEERCRED acceptance")
    path = str(tmp_path / "manager.sock")
    service = Service()
    server = ManagerServer(path, service, gateway_uid=os.geteuid(), socket_gid=os.getegid(),
                           management_error_type=SafeFailure, timeout=1)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, service, path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not os.path.exists(path)


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b'{"x":Infinity}', b'\xff', b'{'])
def test_strict_rpc_json(data):
    with pytest.raises(RpcError, match="valid JSON"):
        decode_json(data)


def test_json_byte_limit_applies_before_parsing():
    with pytest.raises(RpcError) as exc:
        decode_json(b" " * (MAX_REQUEST + 1))
    assert exc.value.code == "request_too_large"


@pytest.mark.parametrize("envelope", [
    {"operation": "run", "payload": {}}, {"operation": "overview", "payload": {}, "command": "id"},
    {"operation": "overview", "payload": []}, {"operation": ["overview"], "payload": {}}, [],
])
def test_rpc_allowlist_and_exact_envelope(envelope):
    with pytest.raises(RpcError):
        validate_request(envelope)


def test_serialization_rejects_non_json_and_oversized_data():
    with pytest.raises(RpcError):
        encode_json({"value": float("nan")}, MAX_RESPONSE)
    with pytest.raises(RpcError):
        encode_json({"value": "a" * MAX_RESPONSE}, MAX_RESPONSE)


def test_framing_handles_split_packets():
    left, right = socket.socketpair()
    with left, right:
        left.settimeout(1)
        right.sendall(b"\x00\x00")
        right.sendall(b"\x00\x02{}")
        assert read_frame(left, 100) == b"{}"


def test_frame_rejects_unbounded_declared_length_before_body():
    left, right = socket.socketpair()
    with left, right:
        left.settimeout(1)
        right.sendall(struct.pack("!I", MAX_REQUEST + 1))
        with pytest.raises(RpcError):
            read_frame(left, MAX_REQUEST)


def test_incomplete_frame_is_not_an_operation():
    left, right = socket.socketpair()
    with left, right:
        right.sendall(struct.pack("!I", 20) + b"{}")
        right.shutdown(socket.SHUT_WR)
        with pytest.raises(RpcError, match="Incomplete"):
            read_frame(left, MAX_REQUEST)


def test_real_unix_rpc_authenticates_peer_and_dispatches(manager):
    server, service, path = manager
    assert call("overview", {}, socket_path=path) == {"revision": 42}
    assert service.calls == [("overview", {})]
    assert os.stat(path).st_mode & 0o777 == 0o660
    assert os.stat(path).st_gid == os.getegid()
    left, right = socket.socketpair(socket.AF_UNIX)
    with left, right:
        assert peer_uid(left) == os.geteuid()


def test_kernel_reports_a_genuinely_different_process_uid():
    if os.name != "posix" or not hasattr(socket, "SO_PEERCRED") or os.geteuid() != 0:
        pytest.skip("Requires Linux root to verify a separate unprivileged peer")
    address = "proxyhub-peer-test-" + uuid.uuid4().hex
    script = ("import os,socket,sys; os.setgroups([]); os.setgid(65534); os.setuid(65534); "
              "s=socket.socket(socket.AF_UNIX); s.connect('\\0'+sys.argv[1]); s.sendall(b'ready'); s.close()")
    with socket.socket(socket.AF_UNIX) as listener:
        listener.settimeout(5)
        listener.bind("\0" + address)
        listener.listen(1)
        child = subprocess.Popen([sys.executable, "-c", script, address], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            connection, _ = listener.accept()
            with connection:
                assert peer_uid(connection) == 65534
                assert connection.recv(5) == b"ready"
            _, stderr = child.communicate(timeout=5)
            assert child.returncode == 0, stderr.decode()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_kernel_peer_rejection_precedes_request_parsing(manager):
    server, service, path = manager
    server.allowed_uids = {os.geteuid() + 100000}
    with pytest.raises(RpcError) as exc:
        call("overview", {}, socket_path=path)
    assert exc.value.code == "access_denied" and not service.calls


def test_unknown_operation_rejected_at_manager_boundary(manager):
    _, service, path = manager
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        connection.connect(path)
        body = b'{"operation":"shell","payload":{"command":"id"}}'
        connection.sendall(struct.pack("!I", len(body)) + body)
        response = decode_json(read_frame(connection, MAX_RESPONSE), MAX_RESPONSE)
    assert response["error"]["code"] == "unknown_operation" and not service.calls


def test_duplicate_request_fields_are_rejected_by_the_manager(manager):
    _, service, path = manager
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        connection.connect(path)
        body = b'{"operation":"overview","payload":{},"payload":{"x":1}}'
        connection.sendall(struct.pack("!I", len(body)) + body)
        response = decode_json(read_frame(connection, MAX_RESPONSE), MAX_RESPONSE)
    assert response["error"]["code"] == "invalid_request" and not service.calls


def test_unexpected_root_error_never_exposes_private_text(manager):
    _, _, path = manager
    with pytest.raises(RpcError) as exc:
        call("overview", {"fail": True}, socket_path=path)
    assert exc.value.code == "internal_error"
    assert "SECRET" not in str(exc.value)


def test_safe_error_and_size_bounds(manager):
    _, _, path = manager
    with pytest.raises(RpcError) as exc:
        call("overview", {"safe_fail": True}, socket_path=path)
    assert exc.value.code == "revision_conflict" and exc.value.message == "Create a fresh preview"
    with pytest.raises(RpcError) as exc:
        call("overview", {"oversized": True}, socket_path=path)
    assert exc.value.code == "response_too_large"


def test_refuses_replacing_active_manager(manager):
    _, _, path = manager
    with pytest.raises(ValueError, match="already listening"):
        ManagerServer(path, Service(), gateway_uid=os.geteuid(), socket_gid=os.getegid(), management_error_type=SafeFailure)


def test_missing_socket_is_a_safe_service_error(tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix socket support is unavailable")
    with pytest.raises(RpcError) as exc:
        call("overview", {}, socket_path=str(tmp_path / "missing.sock"))
    assert exc.value.code == "manager_unavailable"


def test_refuses_unowned_socket_parent_or_regular_file(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX ownership")
    path = tmp_path / "manager.sock"
    path.write_text("existing-data")
    with pytest.raises(ValueError, match="unowned"):
        ManagerServer(str(path), Service(), gateway_uid=os.geteuid(), socket_gid=os.getegid(), management_error_type=SafeFailure)
    assert path.read_text() == "existing-data"
    path.unlink()
    tmp_path.chmod(0o777)
    with pytest.raises(ValueError, match="not writable"):
        ManagerServer(str(path), Service(), gateway_uid=os.geteuid(), socket_gid=os.getegid(), management_error_type=SafeFailure)


def test_duplicate_manager_fails_before_constructing_job_service(monkeypatch):
    from proxyhub import manager as module
    calls = []
    def constructor():
        calls.append("job-service-created")
    def refuse(*args, **kwargs):
        raise ValueError("already listening")
    monkeypatch.setitem(sys.modules, "proxyhub.management", types.SimpleNamespace(
        ManagementError=SafeFailure, OperationService=constructor))
    monkeypatch.setattr(module, "require_root", lambda: None)
    monkeypatch.setattr(module, "ManagerServer", refuse)
    with pytest.raises(ValueError, match="already listening"):
        module.main(["--gateway-uid", "999", "--socket-gid", "999"])
    assert calls == []
