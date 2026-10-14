"""Validate a single-node deployment and render sing-box 1.14.3 configuration.

These functions do not read files, resolve DNS, or change the running system.
Website certificates belong to the separate HTTPS handshake target, never to
the REALITY inbound. Public listeners are owned by the Nginx stream frontend.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import unicodedata
import uuid as uuid_module
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any


_PORT_DEFAULTS = {
    "public_port": 443,
    "http_port": 80,
    "reality_port": 2443,
    "site_tls_port": 8443,
    "subscription_tls_port": 8444,
    "publisher_port": 18890,
}
_REQUIRED = {
    "site_domain", "subscription_domain", "public_address", "uuid",
    "private_key", "public_key", "short_id", "certificate_path", "key_path",
}
_ALLOWED = _REQUIRED | set(_PORT_DEFAULTS) | {"site_title", "node_name"}
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def validate_domain(value: Any, field: str = "domain") -> str:
    """Return a lowercase ASCII DNS name, rejecting local and wildcard names."""
    if not isinstance(value, str) or not value.isascii() or len(value) > 253:
        raise ValueError(f"{field} must be an ASCII DNS name")
    name = value.lower()
    labels = name.split(".")
    if (
        len(labels) < 2
        or not all(_DNS_LABEL.fullmatch(label) for label in labels)
        or not re.search(r"[a-z]", labels[-1])
        or labels[-1] in {"localhost", "local"}
    ):
        raise ValueError(f"{field} must be a non-local DNS name without wildcards")
    return name


def _text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 120
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError(f"{field} must contain 1 to 120 printable characters")
    return value


def _key(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}=?", value):
        raise ValueError(f"{field} must be a 32-byte base64url key")
    try:
        decoded = base64.b64decode(value.rstrip("=") + "=", altchars=b"-_", validate=True)
    except ValueError:
        raise ValueError(f"{field} must be a 32-byte base64url key") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != 32 or canonical != value.rstrip("="):
        raise ValueError(f"{field} must be a canonical 32-byte base64url key")
    return canonical


def validate_managed_certificate_path(value: Any, field: str) -> str:
    """Validate a literal managed certificate path without touching the filesystem."""
    prefix = "/var/lib/proxyhub/certificates/"
    filename = "fullchain.pem" if field == "certificate_path" else "privkey.pem"
    if (
        not isinstance(value, str)
        or not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{8,64}/" + re.escape(filename), value)
    ):
        raise ValueError(f"{field} must be {prefix}<8-64 lowercase hex digits>/{filename}")
    return value


def validate_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated independent copy with normalized fields and defaults.

    All port values are integers and mutually distinct. Keys accept canonical
    unpadded base64url or its single-padding equivalent; output is unpadded.
    This validates key encoding, not the private/public key relationship.
    """
    if not isinstance(spec, Mapping):
        raise ValueError("deployment spec must be an object")
    unknown = set(spec) - _ALLOWED
    missing = _REQUIRED - set(spec)
    if unknown:
        raise ValueError("deployment spec contains unknown fields")
    if missing:
        raise ValueError("deployment spec is missing fields: " + ", ".join(sorted(missing)))
    result = dict(spec)
    for field in ("site_domain", "subscription_domain"):
        result[field] = validate_domain(result[field], field)
    if result["site_domain"] == result["subscription_domain"]:
        raise ValueError("site_domain and subscription_domain must differ")
    address = result["public_address"]
    if not isinstance(address, str) or "%" in address:
        raise ValueError("public_address must be an IP address or DNS hostname")
    try:
        result["public_address"] = str(ipaddress.ip_address(address))
    except ValueError:
        result["public_address"] = validate_domain(address, "public_address")
    for field, default in _PORT_DEFAULTS.items():
        value = result.setdefault(field, default)
        if type(value) is not int or not 1 <= value <= 65535:
            raise ValueError(f"{field} must be an integer between 1 and 65535")
    if len({result[field] for field in _PORT_DEFAULTS}) != len(_PORT_DEFAULTS):
        raise ValueError("all TCP listener ports must be distinct")
    raw_uuid = result["uuid"]
    if not isinstance(raw_uuid, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw_uuid
    ):
        raise ValueError("uuid must use the canonical hyphenated UUID format")
    result["uuid"] = str(uuid_module.UUID(raw_uuid))
    for field in ("private_key", "public_key"):
        result[field] = _key(result[field], field)
    short_id = result["short_id"]
    if not isinstance(short_id, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,8}", short_id):
        raise ValueError("short_id must contain 2 to 16 hexadecimal digits in byte pairs")
    result["short_id"] = short_id.lower()
    for field in ("certificate_path", "key_path"):
        result[field] = validate_managed_certificate_path(result[field], field)
    if PurePosixPath(result["certificate_path"]).parent != PurePosixPath(result["key_path"]).parent:
        raise ValueError("certificate_path and key_path must belong to the same managed certificate")
    result["site_title"] = _text(result.get("site_title", "欢迎来访"), "site_title")
    result["node_name"] = _text(result.get("node_name", "ProxyHub"), "node_name")
    return result


def render_sing_box(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Render a loopback VLESS/REALITY Vision server for the pinned core."""
    value = validate_spec(spec)
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{
            "type": "vless",
            "tag": "proxyhub-reality",
            "listen": "127.0.0.1",
            "listen_port": value["reality_port"],
            "users": [{
                "name": value["node_name"],
                "uuid": value["uuid"],
                "flow": "xtls-rprx-vision",
            }],
            "tls": {
                "enabled": True,
                "server_name": value["site_domain"],
                "reality": {
                    "enabled": True,
                    "handshake": {"server": "127.0.0.1", "server_port": value["site_tls_port"]},
                    "private_key": value["private_key"],
                    "short_id": [value["short_id"]],
                },
            },
        }],
        "outbounds": [{"type": "direct", "tag": "direct"}],
    }
