"""Loopback-only browser gateway; it never opens deployment keys or the state database."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.responses import Response

from .rpc import MAX_REQUEST, MAX_RESPONSE, RpcError, call, decode_json, encode_json, validate_request

PORT = 18765
COOKIE = "proxyhub_session"
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'",
}


def error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status_code=status)


class Boundary:
    """Apply origin/header/body limits before FastAPI or any privileged operation."""
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self.app(scope, receive, send)
            return
        started = False
        async def secure_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in {name.lower().encode() for name in SECURITY_HEADERS}]
                message["headers"] = headers + [(key.lower().encode(), value.encode())
                                                for key, value in SECURITY_HEADERS.items()]
            await send(message)
        async def reject(code: str, message: str, status: int) -> None:
            await error(code, message, status)(scope, receive, secure_send)
        raw_headers = scope.get("headers", [])
        if len(raw_headers) > 128 or sum(len(k) + len(v) for k, v in raw_headers) > 8192:
            await reject("headers_too_large", "Request headers exceed the size limit", 431)
            return
        headers: dict[bytes, list[bytes]] = {}
        for key, value in raw_headers:
            headers.setdefault(key.lower(), []).append(value)
        host_values = headers.get(b"host", [])
        if len(host_values) != 1 or host_values[0].decode("latin1") not in HOSTS:
            await reject("invalid_host", "Use the local ProxyHub tunnel address", 400)
            return
        if scope["method"] not in {"GET", "HEAD", "POST"}:
            await reject("method_not_allowed", "This method is not supported", 405)
            return
        if scope["method"] == "POST":
            origin_values = headers.get(b"origin", [])
            if origin_values != [b"http://" + host_values[0]]:
                await reject("invalid_origin", "A same-origin browser request is required", 403)
                return
            content_types = headers.get(b"content-type", [])
            if len(content_types) != 1 or content_types[0].split(b";", 1)[0].strip().lower() != b"application/json":
                await reject("invalid_content_type", "Use application/json", 415)
                return
            if len(headers.get(b"content-length", [])) > 1:
                await reject("invalid_request", "Invalid request length", 400)
                return
            length = headers.get(b"content-length", [b"0"])[0]
            if not re.fullmatch(rb"[0-9]{1,10}", length):
                await reject("invalid_request", "Invalid request length", 400)
                return
            if int(length) > MAX_REQUEST:
                await reject("request_too_large", "Request exceeds the size limit", 413)
                return
            data = bytearray()
            async def read_body() -> None:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        raise RpcError("invalid_request", "Request disconnected")
                    chunk = message.get("body", b"")
                    if len(data) + len(chunk) > MAX_REQUEST:
                        raise RpcError("request_too_large", "Request exceeds the size limit")
                    data.extend(chunk)
                    if not message.get("more_body", False):
                        break
            try:
                await asyncio.wait_for(read_body(), timeout=5)
                scope["proxyhub.body"] = decode_json(bytes(data))
            except asyncio.TimeoutError:
                await reject("request_timeout", "Request body timed out", 408)
                return
            except RpcError as exc:
                await reject(exc.code, exc.message, 413 if exc.code == "request_too_large" else 400)
                return
        try:
            await self.app(scope, receive, secure_send)
        except Exception:
            # The API handlers contain errors. This fallback handles failures before
            # a response starts; ASGI's outer server redacts any late stream failure.
            if not started:
                await reject("internal_error", "The request could not be completed", 500)


@dataclass
class Session:
    csrf: str
    expires: float


def create_app(config: dict[str, Any], rpc_call: Callable[[str, dict], Any] | None = None) -> FastAPI:
    if not isinstance(config, dict) or set(config) != {"credential_sha256", "port", "session_ttl_seconds", "assets_path"}:
        raise ValueError("Unexpected gateway configuration fields")
    if config["port"] != PORT or type(config["port"]) is not int:
        raise ValueError("The gateway port must be 18765")
    credential_hash = config["credential_sha256"]
    if not isinstance(credential_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", credential_hash):
        raise ValueError("Invalid gateway credential digest")
    ttl = config["session_ttl_seconds"]
    if type(ttl) is not int or not 60 <= ttl <= 3600:
        raise ValueError("Session lifetime must be between 60 and 3600 seconds")
    if not isinstance(config["assets_path"], str):
        raise ValueError("Invalid assets directory")
    assets = Path(config["assets_path"]).resolve(strict=True)
    if (not assets.is_dir() or not (assets / "index.html").is_file()
            or not (assets / "index.html").resolve().is_relative_to(assets)):
        raise ValueError("Panel assets are missing")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Boundary)
    sessions: dict[str, Session] = {}
    attempts: deque[float] = deque()
    invoke = rpc_call or call

    def current(request: Request) -> Session | None:
        now = time.monotonic()
        for token in [token for token, value in sessions.items() if value.expires <= now]:
            del sessions[token]
        # Duplicate cookie names are ambiguous across parsers; do not authenticate them.
        cookie_headers = request.headers.getlist("cookie")
        if len(cookie_headers) > 1 or sum(part.strip().startswith(COOKIE + "=")
                                        for value in cookie_headers for part in value.split(";")) > 1:
            return None
        return sessions.get(request.cookies.get(COOKIE, ""))

    def authenticated(request: Request) -> JSONResponse | None:
        session = current(request)
        if session is None:
            return error("unauthenticated", "Sign in to continue", 401)
        values = request.headers.getlist("x-csrf-token")
        if len(values) != 1 or not hmac.compare_digest(values[0].encode(), session.csrf.encode()):
            return error("invalid_csrf", "Refresh the page and try again", 403)
        return None

    @app.get("/api/session")
    async def session(request: Request) -> dict:
        value = current(request)
        return {"authenticated": True, "csrf": value.csrf} if value else {"authenticated": False}

    @app.post("/api/login")
    async def login(request: Request) -> Response:
        now = time.monotonic()
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            response = error("rate_limited", "Too many sign-in attempts; retry in one minute", 429)
            response.headers["Retry-After"] = "60"
            return response
        attempts.append(now)
        body = request.scope["proxyhub.body"]
        credential = body.get("credential") if isinstance(body, dict) and set(body) == {"credential"} else None
        if not isinstance(credential, str) or not 1 <= len(credential) <= 1024:
            return error("invalid_credentials", "The access credential is invalid", 401)
        if not hmac.compare_digest(hashlib.sha256(credential.encode()).hexdigest(), credential_hash):
            return error("invalid_credentials", "The access credential is invalid", 401)
        current(request)  # Expire old sessions before bounding the collection.
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        if len(sessions) >= 128:
            oldest = min(sessions, key=lambda key: sessions[key].expires)
            del sessions[oldest]
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sessions[token] = Session(csrf, now + ttl)
        response = JSONResponse({"authenticated": True, "csrf": csrf})
        # HTTP is permitted only on loopback through an SSH tunnel. A Secure cookie
        # would break localhost HTTP in clients that do not grant a loopback exception.
        response.set_cookie(COOKIE, token, max_age=ttl, httponly=True, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request) -> Response:
        denied = authenticated(request)
        if denied:
            return denied
        if request.scope["proxyhub.body"] != {}:
            return error("invalid_request", "Logout requires an empty object", 400)
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return response

    @app.post("/api/operation")
    async def operation(request: Request) -> Response:
        denied = authenticated(request)
        if denied:
            return denied
        try:
            name, payload = validate_request(request.scope["proxyhub.body"])
            result = await asyncio.wait_for(asyncio.to_thread(invoke, name, payload), timeout=31)
            encoded = encode_json({"ok": True, "result": result}, MAX_RESPONSE)
            return Response(encoded, media_type="application/json")
        except RpcError as exc:
            status = 503 if exc.code == "manager_unavailable" else 400
            return error(exc.code, exc.message, status)
        except asyncio.TimeoutError:
            return error("manager_timeout", "The management service timed out", 504)
        except Exception:
            return error("internal_error", "The operation could not be completed", 500)

    @app.api_route("/{asset_path:path}", methods=["GET", "HEAD"])
    async def static(request: Request, asset_path: str) -> Response:
        raw = request.scope.get("raw_path", b"")
        if (asset_path.startswith("api/") or asset_path == "api" or len(asset_path) > 512
                or "%" in raw.decode("latin1") or "\\" in asset_path
                or any(part in {".", ".."} for part in asset_path.split("/"))):
            return error("not_found", "This resource does not exist", 404)
        candidate = (assets / asset_path).resolve()
        if not candidate.is_relative_to(assets):
            return error("not_found", "This resource does not exist", 404)
        if not candidate.is_file():
            if Path(asset_path).suffix or asset_path.startswith("assets/"):
                return error("not_found", "This resource does not exist", 404)
            candidate = (assets / "index.html").resolve()
            if not candidate.is_relative_to(assets):
                return error("not_found", "This resource does not exist", 404)
        return FileResponse(candidate)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ProxyHub loopback management gateway")
    parser.add_argument("--config", default="/var/lib/proxyhub/panel/gateway.json")
    args = parser.parse_args(argv)
    with Path(args.config).open("rb") as source:
        config_data = source.read(8193)
    app = create_app(decode_json(config_data, 8192))
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, access_log=False, proxy_headers=False,
                server_header=False, date_header=False, limit_concurrency=32,
                timeout_keep_alive=5, h11_max_incomplete_event_size=8192,
                http="h11", ws="none", log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
