"""Build and atomically persist the sole input accepted by the public publisher.

Each input profile has id, host, token, name, yaml, raw, userinfo, and
update_interval_hours. userinfo may be empty (unknown). upload/download/total
must be supplied together; expire is independently optional. Optional
reset_day/reset_at are management metadata; they never replace expire.
Traffic values are manually supplied bytes, never subscription download sizes.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import quote

MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_BODY_BYTES = 1024 * 1024
MAX_PROFILES = 100
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
GENERATION_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_HOST_PATTERN = re.compile(
    r"(?=.{1,253}(?::[0-9]{1,5})?\Z)"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*(?::[0-9]{1,5})?\Z"
)


class PublicationError(ValueError):
    """An invalid bundle or profile, with no credentials in its message."""


def _object(value: Any, allowed: set[str], required: set[str] | None = None) -> dict:
    required_fields = allowed if required is None else required
    if not isinstance(value, dict) or set(value) - allowed or not required_fields <= set(value):
        raise PublicationError("Invalid or unknown publication fields")
    return value


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PublicationError("Invalid publication integer")
    return value


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PublicationError("Invalid publication text")
    return value


def _body(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PublicationError("Invalid subscription body")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise PublicationError("Invalid subscription text encoding") from exc
    if size > MAX_BODY_BYTES:
        raise PublicationError("Subscription body exceeds limit")
    return value


def _profile(value: Any) -> dict:
    required = {"id", "host", "token", "name", "yaml", "raw", "userinfo", "update_interval_hours"}
    source = _object(value, required | {"reset_day", "reset_at"}, required)
    identifier = _text(source["id"], 64)
    token = _text(source["token"], 128)
    host = _text(source["host"], 260)
    name = _text(source["name"], 200)
    if not IDENTIFIER_PATTERN.fullmatch(identifier) or not TOKEN_PATTERN.fullmatch(token):
        raise PublicationError("Invalid publication identifier or route token")
    if not _HOST_PATTERN.fullmatch(host):
        raise PublicationError("Invalid exact publication host")
    if ":" in host:
        _integer(int(host.rsplit(":", 1)[1]), 1, 65535)
    if "/" in name or "\\" in name:
        raise PublicationError("Invalid download filename")
    userinfo = _object(source["userinfo"], {"upload", "download", "total", "expire"}, set())
    traffic_fields = {"upload", "download", "total"}
    if set(userinfo) & traffic_fields and not traffic_fields <= set(userinfo):
        raise PublicationError("Traffic metadata requires upload, download and total together")
    userinfo = {
        key: _integer(userinfo[key], 0, 2**63 - 1)
        for key in ("upload", "download", "total", "expire") if key in userinfo
    }
    interval = _integer(source["update_interval_hours"], 1, 168)
    reset_day = source.get("reset_day")
    reset_at = source.get("reset_at")
    if reset_day is not None:
        reset_day = _integer(reset_day, 1, 31)
    if reset_at is not None:
        reset_at = _integer(reset_at, 0, 2**63 - 1)
    yaml_body = _body(source["yaml"])
    raw_body = _body(source["raw"])
    try:
        decoded = base64.b64decode(raw_body, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise PublicationError("Invalid raw subscription encoding") from exc
    if not decoded.splitlines() or any(not line.startswith("vless://") for line in decoded.splitlines()):
        raise PublicationError("Unsupported raw subscription protocol")
    responses = {}
    for format_name, suffix, content_type, body in (
        ("yaml", "mihomo.yaml", "text/yaml; charset=utf-8", yaml_body),
        ("raw", "raw", "text/plain; charset=utf-8", raw_body),
    ):
        extension = ".yaml" if format_name == "yaml" else ".txt"
        headers = {
            "Content-Type": content_type,
            "Content-Disposition": f"attachment; filename=\"subscription{extension}\"; filename*=UTF-8''{quote(name + extension, safe='')}",
            "Profile-Update-Interval": str(interval),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if userinfo:
            headers["Subscription-Userinfo"] = "; ".join(f"{key}={value}" for key, value in userinfo.items())
        responses[format_name] = {"path": f"/sub/{token}/{suffix}", "headers": headers, "body": body}
    return {
        "id": identifier, "host": host, "token": token, "name": name,
        "management": {
            "traffic_source": "manual", "userinfo": userinfo,
            "update_interval_hours": interval, "reset_day": reset_day, "reset_at": reset_at,
        },
        "responses": responses,
    }


def build_bundle(profiles: list[dict], generation: str) -> dict:
    """Validate profiles and bind exact routes, bodies and metadata in one revision."""
    if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(generation):
        raise PublicationError("Invalid publication generation")
    if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
        raise PublicationError("Invalid publication profile count")
    built = [_profile(profile) for profile in profiles]
    identifiers: set[str] = set()
    routes: set[tuple[str, str]] = set()
    for profile in built:
        if profile["id"] in identifiers:
            raise PublicationError("Duplicate publication identifier")
        identifiers.add(profile["id"])
        for response in profile["responses"].values():
            key = (profile["host"], response["path"])
            if key in routes:
                raise PublicationError("Duplicate publication route")
            routes.add(key)
    bundle = {"version": 1, "generation": generation, "profiles": built}
    if len(_serialize(bundle)) > MAX_BUNDLE_BYTES:
        raise PublicationError("Publication bundle exceeds limit")
    return bundle


def validate_bundle(bundle: Any) -> dict:
    """Rebuild all managed fields so a corrupted bundle cannot inject HTTP headers."""
    source = _object(bundle, {"version", "generation", "profiles"})
    if type(source["version"]) is not int or source["version"] != 1 or not isinstance(source["profiles"], list):
        raise PublicationError("Unsupported publication bundle")
    inputs = []
    for item in source["profiles"]:
        item = _object(item, {"id", "host", "token", "name", "management", "responses"})
        metadata = _object(item["management"], {"traffic_source", "userinfo", "update_interval_hours", "reset_day", "reset_at"})
        if metadata["traffic_source"] != "manual":
            raise PublicationError("Unsupported traffic metadata source")
        responses = _object(item["responses"], {"yaml", "raw"})
        for response in responses.values():
            _object(response, {"path", "headers", "body"})
        inputs.append({
            "id": item["id"], "host": item["host"], "token": item["token"], "name": item["name"],
            "yaml": responses["yaml"]["body"], "raw": responses["raw"]["body"],
            "userinfo": metadata["userinfo"], "update_interval_hours": metadata["update_interval_hours"],
            "reset_day": metadata["reset_day"], "reset_at": metadata["reset_at"],
        })
    rebuilt = build_bundle(inputs, source["generation"])
    if rebuilt != source:
        raise PublicationError("Publication bundle managed fields do not match")
    return rebuilt


def _serialize(bundle: dict) -> bytes:
    try:
        return (json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PublicationError("Invalid publication encoding") from exc


def save_bundle(path: str | Path, bundle: dict) -> None:
    """fsync a mode-0600 sibling file, atomically replace, then fsync its directory.

    Directory fsync is available on POSIX deployment hosts. Windows supports the
    same replacement but does not expose directory fsync through this API.
    The caller owns creation and permissions of the containing directory.
    """
    destination = Path(path)
    data = _serialize(validate_bundle(bundle))
    if destination.is_symlink():
        raise PublicationError("Refusing a symlink publication destination")
    fd, temporary = tempfile.mkstemp(prefix=".proxyhub-bundle-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name == "posix":
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
