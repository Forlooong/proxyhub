"""Read-only loopback HTTP runtime serving immutable publication snapshots.

The only persistent input is the bounded JSON bundle. There is no outbound
fetch, write endpoint, access log, manager socket or database dependency.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import signal
import socket
import sys
import threading
from types import MappingProxyType
from typing import Any, Mapping

from .publication import MAX_BUNDLE_BYTES, PublicationError, validate_bundle

MAX_HEADER_BYTES = 8192
REQUEST_TIMEOUT = 5
MAX_CONNECTIONS = 32
_PATH = re.compile(r"/sub/[A-Za-z0-9_-]{32,128}/(?:mihomo\.yaml|raw)\Z")


@dataclass(frozen=True)
class Response:
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class Snapshot:
    generation: str
    routes: Mapping[tuple[str, str], Response]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError("Duplicate publication JSON field")
        result[key] = value
    return result


def load_snapshot(path: str | Path) -> Snapshot:
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_BUNDLE_BYTES + 1)
    if len(data) > MAX_BUNDLE_BYTES:
        raise PublicationError("Publication bundle exceeds limit")
    try:
        bundle = validate_bundle(json.loads(data, object_pairs_hook=_unique_object))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PublicationError("Invalid publication bundle") from exc
    routes = {}
    for profile in bundle["profiles"]:
        for response in profile["responses"].values():
            headers = tuple(response["headers"].items()) + (("X-ProxyHub-Generation", bundle["generation"]),)
            routes[(profile["host"], response["path"])] = Response(headers, response["body"].encode("utf-8"))
    return Snapshot(bundle["generation"], MappingProxyType(routes))


class _HeaderReader:
    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.remaining = MAX_HEADER_BYTES

    def readline(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            raise http.client.LineTooLong("request headers")
        value = self.reader.readline(min(size, self.remaining + 1) if size >= 0 else self.remaining + 1)
        self.remaining -= len(value)
        if self.remaining < 0:
            raise http.client.LineTooLong("request headers")
        return value


class PublisherHandler(BaseHTTPRequestHandler):
    server: PublisherServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        self.request.settimeout(REQUEST_TIMEOUT)
        super().setup()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._empty(code)

    def _empty(self, code: int) -> None:
        self.close_connection = True
        self.send_response_only(code)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if code == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()

    def parse_request(self) -> bool:
        reader = self.rfile
        self.rfile = _HeaderReader(reader)  # type: ignore[assignment]
        try:
            parsed = super().parse_request()
        finally:
            self.rfile = reader
        self.close_connection = True
        if not parsed:
            return False
        if len(self.raw_requestline) > 2048 or self.request_version not in {"HTTP/1.0", "HTTP/1.1"}:
            self._empty(400)
            return False
        if self.command not in {"GET", "HEAD"}:
            self._empty(405)
            return False
        # BaseHTTPRequestHandler normalizes leading //; routing must use the exact target.
        raw_parts = self.raw_requestline.split()
        if len(raw_parts) != 3 or raw_parts[1] != self.path.encode("ascii", errors="replace"):
            self._empty(404)
            return False
        if self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Content-Length"):
            self._empty(400)
            return False
        return True

    def handle_expect_100(self) -> bool:
        self._empty(417)
        return False

    def do_GET(self) -> None:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or not _PATH.fullmatch(self.path):
            self._empty(404)
            return
        snapshot = self.server.snapshot
        response = snapshot.routes.get((hosts[0], self.path))
        if response is None:
            self._empty(404)
            return
        self.send_response_only(200)
        for name, value in response.headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(response.body)

    do_HEAD = do_GET


class PublisherServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, address: tuple[str, int], bundle_path: str | Path):
        try:
            if not ipaddress.ip_address(address[0]).is_loopback:
                raise ValueError
        except ValueError as exc:
            raise PublicationError("Publisher requires a loopback IP address") from exc
        self.bundle_path = Path(bundle_path)
        self._snapshot = load_snapshot(self.bundle_path)
        self._snapshot_lock = threading.Lock()
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.reload_requested = threading.Event()
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, PublisherHandler)

    @property
    def snapshot(self) -> Snapshot:
        with self._snapshot_lock:
            return self._snapshot

    def reload_bundle(self) -> bool:
        try:
            candidate = load_snapshot(self.bundle_path)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return False
        with self._snapshot_lock:
            self._snapshot = candidate
        return True

    def service_actions(self) -> None:
        if self.reload_requested.is_set():
            self.reload_requested.clear()
            if not self.reload_bundle():
                print("publisher: reload failed; retaining current bundle", file=sys.stderr)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Interrupted clients must not put request paths or tracebacks in logs.
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve an immutable ProxyHub subscription bundle on loopback")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", default=18890, type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        server = PublisherServer((args.listen, args.port), args.bundle)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("publisher: startup failed", file=sys.stderr)
        return 2
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: server.reload_requested.set())
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
