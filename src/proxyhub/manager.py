"""Root RPC service. Only a local, kernel-identified gateway may call typed operations."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import socketserver
import stat
import struct
import threading
from typing import Any

from .core import require_root
from .rpc import MAX_REQUEST, MAX_RESPONSE, SOCKET_PATH, RpcError, decode_json, read_frame, validate_request, write_frame


def peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise RpcError("access_denied", "Kernel peer authentication is unavailable")
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", credentials)[1]


class ManagerServer(socketserver.ThreadingMixIn, getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, path: str, service: Any, *, gateway_uid: int, socket_gid: int,
                 management_error_type: type[Exception], timeout: float = 30):
        if not hasattr(socket, "AF_UNIX") or not hasattr(socket, "SO_PEERCRED"):
            raise ValueError("The manager requires Linux Unix sockets and SO_PEERCRED")
        self.service = service
        self.allowed_uids = {0, gateway_uid}
        self.management_error_type = management_error_type
        self.request_timeout = timeout
        self._slots = threading.BoundedSemaphore(16)
        destination = Path(path)
        parent = destination.parent
        if parent.resolve() != parent or not parent.is_dir():
            raise ValueError("RPC socket parent must be an existing, direct directory")
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & 0o022:
            raise ValueError("RPC socket parent must be owned by the manager and not writable by others")
        if destination.exists() or destination.is_symlink():
            info = destination.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("Refusing an unowned RPC socket destination")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(path)
                except ConnectionRefusedError:
                    destination.unlink()
                else:
                    raise ValueError("An RPC manager is already listening")
        super().__init__(path, _Handler, bind_and_activate=False)
        try:
            self.server_bind()
            os.chown(path, os.geteuid(), socket_gid)
            os.chmod(path, 0o660)
            self._socket_inode = destination.stat().st_ino
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Never log request content, exceptions containing a certificate key, or URLs.
        return

    def server_close(self) -> None:
        super().server_close()
        path = Path(self.server_address)
        try:
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_ino == getattr(self, "_socket_inode", None):
                path.unlink()
        except FileNotFoundError:
            pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: ManagerServer = self.server
        self.request.settimeout(server.request_timeout)
        try:
            if peer_uid(self.request) not in server.allowed_uids:
                raise RpcError("access_denied", "This peer may not access the management service")
            operation, payload = validate_request(decode_json(read_frame(self.request, MAX_REQUEST)))
            result = server.service.handle(operation, payload)
            write_frame(self.request, {"ok": True, "result": result}, MAX_RESPONSE)
            return
        except RpcError as exc:
            code, message = exc.code, exc.message
        except server.management_error_type as exc:
            code = getattr(exc, "code", "operation_failed")
            message = getattr(exc, "message", "The operation failed")
            if (not isinstance(code, str) or not isinstance(message, str)
                    or len(code) > 64 or len(message) > 512):
                code, message = "internal_error", "The operation failed"
        except (OSError, TimeoutError):
            return
        except Exception:
            code, message = "internal_error", "The operation failed"
        try:
            write_frame(self.request, {"ok": False, "error": {"code": code, "message": message}}, MAX_RESPONSE)
        except (OSError, RpcError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ProxyHub privileged local manager")
    parser.add_argument("--gateway-uid", type=int, required=True)
    parser.add_argument("--socket-gid", type=int, required=True)
    args = parser.parse_args(argv)
    require_root()
    if args.gateway_uid <= 0 or args.socket_gid <= 0:
        parser.error("Gateway UID and socket group must be non-root IDs")
    from .management import ManagementError, OperationService
    # Binding is the process ownership gate. Constructing OperationService first
    # would let a rejected second process mark the running process's jobs interrupted.
    with ManagerServer(SOCKET_PATH, None, gateway_uid=args.gateway_uid,
                       socket_gid=args.socket_gid, management_error_type=ManagementError) as server:
        service = OperationService()
        server.service = service
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            close = getattr(service, "close", None)
            if close:
                close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
