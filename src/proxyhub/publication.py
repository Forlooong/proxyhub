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
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import quote

MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_BODY_BYTES = 1024 * 1024
MAX_PROFILES = 100
MAX_SECURITY_BYTES = 2 * 1024 * 1024
MAX_SECURITY_ITEMS = 10000
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
GENERATION_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
PATH_PREFIX_PATTERN = re.compile(r"/(?:[A-Za-z0-9_-]{1,64}/)*[A-Za-z0-9_-]{1,64}\Z")
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


def _route(value: Any) -> dict:
    source = _object(value, {"id", "host", "path_prefix"}, {"host", "path_prefix"})
    host = _text(source["host"], 260)
    prefix = _text(source["path_prefix"], 200)
    if not _HOST_PATTERN.fullmatch(host) or not PATH_PREFIX_PATTERN.fullmatch(prefix):
        raise PublicationError("Invalid exact publication route")
    if ":" in host:
        _integer(int(host.rsplit(":", 1)[1]), 1, 65535)
    result = {"host": host, "path_prefix": prefix}
    if "id" in source:
        if not isinstance(source["id"], str) or not IDENTIFIER_PATTERN.fullmatch(source["id"]):
            raise PublicationError("Invalid publication alias identifier")
        result["id"] = source["id"]
    return result


def _profile(value: Any) -> dict | None:
    required = {"id", "host", "token", "name", "userinfo", "update_interval_hours"}
    extensions = {"path_prefix", "aliases", "formats", "enabled"}
    source = _object(value, required | {"yaml", "raw", "reset_day", "reset_at"} | extensions, required)
    extended = bool(extensions & set(source))
    identifier = _text(source["id"], 64)
    token = _text(source["token"], 128)
    host = _text(source["host"], 260)
    name = _text(source["name"], 200)
    if not IDENTIFIER_PATTERN.fullmatch(identifier) or not TOKEN_PATTERN.fullmatch(token):
        raise PublicationError("Invalid publication identifier or route token")
    primary = _route({"host": host, "path_prefix": source.get("path_prefix", "/sub")})
    aliases = source.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 32:
        raise PublicationError("Invalid publication aliases")
    aliases = [_route(alias) for alias in aliases]
    routes = [primary, *aliases]
    if len({(item["host"], item["path_prefix"]) for item in routes}) != len(routes):
        raise PublicationError("Duplicate publication alias")
    formats = source.get("formats", ["yaml", "raw"])
    if (not isinstance(formats, list) or not formats or len(formats) > 2
            or any(not isinstance(item, str) or item not in {"yaml", "raw"} for item in formats)
            or len(set(formats)) != len(formats)):
        raise PublicationError("Invalid publication formats")
    enabled = source.get("enabled", True)
    if type(enabled) is not bool:
        raise PublicationError("Invalid publication enabled state")
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
    if not enabled:
        return None
    bodies = {}
    for kind in formats:
        if kind not in source:
            raise PublicationError("Missing enabled subscription format")
        bodies[kind] = _body(source[kind])
    if "raw" in bodies:
        try:
            decoded = base64.b64decode(bodies["raw"], validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeError) as exc:
            raise PublicationError("Invalid raw subscription encoding") from exc
        protocols = ("vless://", "hysteria2://", "ss://", "trojan://", "vmess://", "socks5://", "tuic://", "anytls://")
        if not decoded.splitlines() or any(not line.startswith(protocols) for line in decoded.splitlines()):
            raise PublicationError("Unsupported raw subscription protocol")
    responses = {}
    for format_name in formats:
        suffix = "mihomo.yaml" if format_name == "yaml" else "raw"
        content_type = "text/yaml; charset=utf-8" if format_name == "yaml" else "text/plain; charset=utf-8"
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
        for index, route in enumerate(routes):
            response = {"path": f"{route['path_prefix']}/{token}/{suffix}",
                        "headers": dict(headers), "body": bodies[format_name]}
            if extended:
                response.update(host=route["host"], format=format_name)
            responses[format_name if index == 0 else f"alias-{index-1}-{format_name}"] = response
    result = {
        "id": identifier, "host": host, "token": token, "name": name,
        "management": {
            "traffic_source": "manual", "userinfo": userinfo,
            "update_interval_hours": interval, "reset_day": reset_day, "reset_at": reset_at,
        },
        "responses": responses,
    }
    if extended:
        result.update(path_prefix=primary["path_prefix"], aliases=aliases, formats=list(formats))
    return result


def iter_routes(profile: dict):
    """Yield uniform exact routes for both legacy and current bundle profiles."""
    for name, response in profile["responses"].items():
        yield {**response, "host": response.get("host", profile["host"]),
               "format": response.get("format", name),
               "path_prefix": response["path"].rsplit("/", 2)[0]}


def build_bundle(profiles: list[dict], generation: str) -> dict:
    """Validate profiles and bind exact routes, bodies and metadata in one revision."""
    if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(generation):
        raise PublicationError("Invalid publication generation")
    if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
        raise PublicationError("Invalid publication profile count")
    built = [item for profile in profiles if (item := _profile(profile)) is not None]
    identifiers: set[str] = set()
    routes: set[tuple[str, str]] = set()
    for profile in built:
        if profile["id"] in identifiers:
            raise PublicationError("Duplicate publication identifier")
        identifiers.add(profile["id"])
        for response in iter_routes(profile):
            key = (response["host"], response["path"])
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
        required = {"id", "host", "token", "name", "management", "responses"}
        item = _object(item, required | {"path_prefix", "aliases", "formats"}, required)
        extended = "formats" in item
        metadata = _object(item["management"], {"traffic_source", "userinfo", "update_interval_hours", "reset_day", "reset_at"})
        if metadata["traffic_source"] != "manual":
            raise PublicationError("Unsupported traffic metadata source")
        formats = item.get("formats", ["yaml", "raw"])
        if not isinstance(formats, list) or any(not isinstance(kind, str) or kind not in {"yaml", "raw"} for kind in formats):
            raise PublicationError("Invalid publication formats")
        if not isinstance(item.get("aliases", []), list):
            raise PublicationError("Invalid publication aliases")
        response_keys = set(formats) | {f"alias-{index}-{kind}" for index in range(len(item.get("aliases", []))) for kind in formats}
        responses = _object(item["responses"], response_keys)
        for response in responses.values():
            _object(response, {"path", "headers", "body", "host", "format"} if extended else {"path", "headers", "body"})
        entry = {
            "id": item["id"], "host": item["host"], "token": item["token"], "name": item["name"],
            "userinfo": metadata["userinfo"], "update_interval_hours": metadata["update_interval_hours"],
            "reset_day": metadata["reset_day"], "reset_at": metadata["reset_at"],
        }
        entry.update({kind: responses[kind]["body"] for kind in formats})
        if extended:
            entry.update(path_prefix=item["path_prefix"], aliases=item["aliases"], formats=formats)
        inputs.append(entry)
    rebuilt = build_bundle(inputs, source["generation"])
    if rebuilt != source:
        raise PublicationError("Publication bundle managed fields do not match")
    return rebuilt


def _serialize(bundle: dict) -> bytes:
    try:
        return (json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PublicationError("Invalid publication encoding") from exc


def token_digest(token: str) -> str:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise PublicationError("Invalid publication route token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def alias_digest(host: str, path_prefix: str) -> str:
    route = _route({"host": host, "path_prefix": path_prefix})
    return hashlib.sha256((route["host"] + "\0" + route["path_prefix"]).encode("ascii")).hexdigest()


def validate_security(value: Any) -> dict:
    source = _object(value, {"schema_version", "generation", "revoked_tokens", "retired_aliases"})
    if type(source["schema_version"]) is not int or source["schema_version"] != 1:
        raise PublicationError("Unsupported publication security state")
    generation = _integer(source["generation"], 0, 2**63 - 1)
    result = {"schema_version": 1, "generation": generation}
    for name in ("revoked_tokens", "retired_aliases"):
        values = source[name]
        if (not isinstance(values, list) or len(values) > MAX_SECURITY_ITEMS
                or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in values)
                or len(set(values)) != len(values)):
            raise PublicationError("Invalid publication security entries")
        result[name] = sorted(values)
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise PublicationError("Duplicate publication security field")
        result[name] = value
    return result


def load_security(path: str | Path) -> dict:
    source = Path(path)
    if source.is_symlink():
        raise PublicationError("Refusing a symlink publication security state")
    with source.open("rb") as handle:
        data = handle.read(MAX_SECURITY_BYTES + 1)
    if len(data) > MAX_SECURITY_BYTES:
        raise PublicationError("Publication security state exceeds limit")
    try:
        return validate_security(json.loads(data, object_pairs_hook=_unique_object))
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise PublicationError("Invalid publication security state") from exc


def save_security(path: str | Path, security: dict) -> None:
    """Persist the independent deny ledger; callers must retain it on rollback."""
    security = validate_security(security)
    destination = Path(path)
    if destination.exists() or destination.is_symlink():
        current = load_security(destination)
        if (security["generation"] < current["generation"]
                or any(not set(current[name]) <= set(security[name]) for name in ("revoked_tokens", "retired_aliases"))
                or security != current and security["generation"] == current["generation"]):
            raise PublicationError("Publication security state cannot go backwards")
    data = _serialize(security)
    if len(data) > MAX_SECURITY_BYTES:
        raise PublicationError("Publication security state exceeds limit")
    _save_document(destination, data, ".proxyhub-security-", 0o640, preserve_owner=True)


def advance_security(path: str | Path, tokens=(), aliases=()) -> dict:
    """Add tombstones once under the caller's existing exclusive operation lock."""
    current = load_security(path)
    revoked = set(current["revoked_tokens"]) | {token_digest(token) for token in tokens}
    retired = set(current["retired_aliases"]) | {alias_digest(item["host"], item["path_prefix"]) for item in aliases}
    if revoked == set(current["revoked_tokens"]) and retired == set(current["retired_aliases"]):
        return current
    updated = {"schema_version": 1, "generation": current["generation"] + 1,
               "revoked_tokens": sorted(revoked), "retired_aliases": sorted(retired)}
    save_security(path, updated)
    return updated


def route_allowed(security: dict, host: str, path_prefix: str, token: str) -> bool:
    return (token_digest(token) not in security["revoked_tokens"]
            and alias_digest(host, path_prefix) not in security["retired_aliases"])


def save_bundle(path: str | Path, bundle: dict) -> None:
    """fsync a mode-0600 sibling file, atomically replace, then fsync its directory.

    Directory fsync is available on POSIX deployment hosts. Windows supports the
    same replacement but does not expose directory fsync through this API.
    The caller owns creation and permissions of the containing directory.
    """
    data = _serialize(validate_bundle(bundle))
    _save_document(Path(path), data, ".proxyhub-bundle-", 0o600)


def _save_document(destination: Path, data: bytes, prefix: str, mode: int,
                   *, preserve_owner: bool = False) -> None:
    if destination.is_symlink():
        raise PublicationError("Refusing a symlink publication destination")
    fd, temporary = tempfile.mkstemp(prefix=prefix, dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            if preserve_owner and hasattr(os, "fchown") and destination.exists():
                owner = destination.stat()
                os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
            os.chmod(temporary, mode)
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
