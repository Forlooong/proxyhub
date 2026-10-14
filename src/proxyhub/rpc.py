"""Bounded, typed Unix RPC shared by the unprivileged gateway and root manager."""
from __future__ import annotations

import json
import socket
import struct
import time
from typing import Any

SOCKET_PATH = "/run/proxyhub/manager.sock"
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 1024 * 1024
OPERATIONS = frozenset({
    "overview", "jobs.list", "job.get", "ssh.inspect", "plan.create",
    "plan.apply", "subscription.reveal", "deployment.recover",
})


class RpcError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode_json(data: bytes, maximum: int = MAX_REQUEST) -> Any:
    if len(data) > maximum:
        raise RpcError("request_too_large", "Request exceeds the size limit")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RpcError("invalid_request", "Expected valid JSON without duplicate fields") from exc


def validate_request(value: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"operation", "payload"}:
        raise RpcError("invalid_request", "Expected operation and payload fields")
    operation, payload = value["operation"], value["payload"]
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise RpcError("unknown_operation", "This operation is not supported")
    if not isinstance(payload, dict):
        raise RpcError("invalid_request", "Operation payload must be an object")
    return operation, payload


def encode_json(value: Any, maximum: int) -> bytes:
    try:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise RpcError("internal_error", "Unable to encode the operation result") from exc
    if len(data) > maximum:
        raise RpcError("response_too_large", "Operation result exceeds the size limit")
    return data


def _read_exact(connection: socket.socket, length: int, deadline: float | None = None) -> bytes:
    output = bytearray()
    while len(output) < length:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RPC message timed out")
            connection.settimeout(remaining)
        chunk = connection.recv(min(65536, length - len(output)))
        if not chunk:
            raise RpcError("invalid_request", "Incomplete RPC message")
        output.extend(chunk)
    return bytes(output)


def read_frame(connection: socket.socket, maximum: int) -> bytes:
    timeout = connection.gettimeout()
    deadline = time.monotonic() + timeout if timeout is not None else None
    length = struct.unpack("!I", _read_exact(connection, 4, deadline))[0]
    if length == 0 or length > maximum:
        raise RpcError("request_too_large", "RPC message exceeds the size limit")
    return _read_exact(connection, length, deadline)


def write_frame(connection: socket.socket, value: Any, maximum: int) -> None:
    data = encode_json(value, maximum)
    connection.sendall(struct.pack("!I", len(data)) + data)


def call(operation: str, payload: dict[str, Any], *, socket_path: str = SOCKET_PATH,
         timeout: float = 30) -> Any:
    envelope = {"operation": operation, "payload": payload}
    validate_request(envelope)
    encoded = encode_json(envelope, MAX_REQUEST)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(socket_path)
            connection.sendall(struct.pack("!I", len(encoded)) + encoded)
            response = decode_json(read_frame(connection, MAX_RESPONSE), MAX_RESPONSE)
    except (OSError, TimeoutError) as exc:
        raise RpcError("manager_unavailable", "The management service is unavailable") from exc
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise RpcError("invalid_response", "The management service returned an invalid response")
    if response["ok"] and set(response) == {"ok", "result"}:
        return response["result"]
    error = response.get("error")
    if (response["ok"] is False and set(response) == {"ok", "error"}
            and isinstance(error, dict) and set(error) == {"code", "message"}
            and isinstance(error["code"], str) and isinstance(error["message"], str)
            and len(error["code"]) <= 64 and len(error["message"]) <= 512):
        raise RpcError(error["code"], error["message"])
    raise RpcError("invalid_response", "The management service returned an invalid response")
