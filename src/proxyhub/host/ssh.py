"""Append a public key to the effective OpenSSH AuthorizedKeysFile safely.

Plans are JSON data, bound to a local POSIX account, connection context, effective
``sshd -T -C`` output and filesystem snapshots. Apply re-plans before and after
advisory directory locks, and immediately before replacement. Other writers must
cooperate with the locks for complete serialization; arbitrary concurrent root or
account-owner writes cannot be made transactional by POSIX rename alone.

Existing bytes and safe ownership/modes are preserved, including modes stricter
than 0600/0700. Newly created directories/files use 0700/0600. No authentication
success is claimed: a separate SSH connection must verify the new key.
"""

from __future__ import annotations

import base64
import binascii
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import os
from pathlib import PurePosixPath
import re
import shlex
import stat
import struct
import subprocess
from typing import Any, Callable, Mapping
import uuid


class SSHKeyError(ValueError):
    """Invalid or unsupported authorized_keys input."""


class SSHSafetyError(RuntimeError):
    """Unsafe configuration, conflicting key, or changed host state."""


_KEY_TYPES = {
    "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
}
_FORWARD_FLAGS = {"agent-forwarding", "port-forwarding", "x11-forwarding", "pty", "user-rc"}
_FLAG_DEFAULTS = {**dict.fromkeys(_FORWARD_FLAGS, True), "touch-required": True, "verify-required": False}
_VALUE_OPTIONS = {"command", "principals", "from", "expiry-time", "environment", "permitopen", "permitlisten", "tunnel"}
_MAX_FILE_BYTES = 16 * 1024 * 1024
_SSHD = "/usr/sbin/sshd"


@dataclass(frozen=True)
class AuthorizedKey:
    keytype: str
    blob: bytes
    options: tuple[tuple[str, str | None], ...]
    comment: str
    line: str

    @property
    def identity(self) -> tuple[str, bytes]:
        return self.keytype, self.blob

    @property
    def fingerprint(self) -> str:
        digest = base64.b64encode(hashlib.sha256(self.blob).digest()).decode("ascii").rstrip("=")
        return "SHA256:" + digest


def _field(text: str, start: int = 0) -> tuple[str, int]:
    """OpenSSH quoting: only backslash followed by a quote is unescaped."""
    while start < len(text) and text[start] in " \t":
        start += 1
    end, quoted = start, False
    while end < len(text):
        char = text[end]
        if quoted and char == "\\" and end + 1 < len(text) and text[end + 1] == '"':
            end += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char in " \t" and not quoted:
            break
        end += 1
    if quoted:
        raise SSHKeyError("Unterminated authorized_keys option quote")
    return text[start:end], end


def _port_rule(name: str, value: str) -> str:
    if name == "permitlisten" and ":" not in value:
        value = "*:" + value
    match = re.fullmatch(r"(\[[^\]\s]+\]|[^:\s]+):(\*|[0-9]+)", value)
    if not match or (match[2] != "*" and not 1 <= int(match[2]) <= 65535):
        raise SSHKeyError("Invalid forwarding permission option")
    host = match[1]
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError as exc:
            raise SSHKeyError("Invalid bracketed forwarding host") from exc
    return host + ":" + ("*" if match[2] == "*" else str(int(match[2])))


def _expiry(value: str) -> str:
    zulu = value.endswith("Z")
    digits = value[:-1] if zulu else value
    fmt = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(digits))
    if not fmt or not digits.isascii() or not digits.isdigit():
        raise SSHKeyError("Invalid expiry-time option")
    try:
        parsed = datetime.strptime(digits, fmt)
        if zulu:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = int(parsed.timestamp())
    except (ValueError, OverflowError, OSError) as exc:
        raise SSHKeyError("Invalid expiry-time option") from exc
    if timestamp <= 0:
        raise SSHKeyError("Invalid expiry-time option")
    return str(timestamp)


def _options(text: str) -> tuple[tuple[str, str | None], ...]:
    if not text:
        return ()
    flags = dict(_FLAG_DEFAULTS)
    values: dict[str, str | None] = {}
    repeated: set[tuple[str, str]] = set()
    environment: dict[str, str] = {}
    pos = 0
    while pos < len(text):
        match = re.match(r"[A-Za-z][A-Za-z0-9-]*", text[pos:])
        if not match:
            raise SSHKeyError("Invalid authorized_keys option name")
        name = match[0].lower()
        pos += len(match[0])
        value = None
        if pos < len(text) and text[pos] == "=":
            pos += 1
            if pos == len(text) or text[pos] != '"':
                raise SSHKeyError("Option values must be double quoted")
            pos += 1
            chars: list[str] = []
            while pos < len(text) and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < len(text) and text[pos + 1] == '"':
                    pos += 1
                chars.append(text[pos])
                pos += 1
            if pos == len(text):
                raise SSHKeyError("Unterminated authorized_keys option quote")
            value = "".join(chars)
            pos += 1
        if name in _VALUE_OPTIONS:
            if value is None:
                raise SSHKeyError("This authorized_keys option requires a quoted value")
            if name in {"command", "principals", "from"}:
                if name in values:
                    raise SSHKeyError("Duplicate singleton authorized_keys option")
                if name != "command" and not value:
                    raise SSHKeyError("Empty restriction option")
                values[name] = value
            elif name == "environment":
                env_name, separator, env_value = value.partition("=")
                if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
                    raise SSHKeyError("Invalid environment option")
                # OpenSSH uses the first occurrence of an environment name.
                environment.setdefault(env_name, env_value)
            elif name == "expiry-time":
                expiry = _expiry(value)
                if name not in values or int(expiry) < int(str(values[name])):
                    values[name] = expiry
            elif name in {"permitopen", "permitlisten"}:
                repeated.add((name, _port_rule(name, value)))
            elif name == "tunnel":
                if value != "any" and (not re.fullmatch(r"[0-9]+", value) or int(value) > 2147483646):
                    raise SSHKeyError("Invalid tunnel option")
                values[name] = "any" if value == "any" else str(int(value))
        elif value is not None:
            raise SSHKeyError("Unknown or invalid valued authorized_keys option")
        elif name == "restrict":
            values[name] = None
            for flag in _FORWARD_FLAGS:
                flags[flag] = False
        elif name == "cert-authority":
            values[name] = None
        else:
            flag = name.removeprefix("no-")
            if flag not in flags:
                raise SSHKeyError("Unknown authorized_keys option")
            flags[flag] = not name.startswith("no-")
        if pos < len(text):
            if text[pos] != "," or pos + 1 == len(text):
                raise SSHKeyError("Invalid authorized_keys option separator")
            pos += 1
    items = list(values.items()) + list(repeated)
    items.extend(("environment", name + "=" + value) for name, value in environment.items())
    items.extend((name, "yes" if value else "no") for name, value in flags.items() if value != _FLAG_DEFAULTS[name])
    return tuple(sorted(items, key=lambda item: (item[0], item[1] or "")))


class _WireReader:
    def __init__(self, blob: bytes):
        self.blob, self.pos = blob, 0

    def string(self) -> bytes:
        if self.pos + 4 > len(self.blob):
            raise SSHKeyError("Truncated SSH public key blob")
        size = struct.unpack_from(">I", self.blob, self.pos)[0]
        self.pos += 4
        if size > len(self.blob) - self.pos:
            raise SSHKeyError("Truncated SSH public key field")
        result = self.blob[self.pos:self.pos + size]
        self.pos += size
        return result

    def positive_mpint(self) -> int:
        value = self.string()
        if not value or value[0] & 0x80 or (value[0] == 0 and (len(value) == 1 or value[1] < 0x80)):
            raise SSHKeyError("Invalid SSH positive mpint")
        return int.from_bytes(value, "big")


_CURVES = {
    "nistp256": (
        32,
        0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF,
        0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B,
    ),
    "nistp384": (
        48,
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFFFF0000000000000000FFFFFFFF,
        0xB3312FA7E23EE7E4988E056BE3F82D19181D9C6EFE8141120314088F5013875AC656398D8A2ED19D2A85C8EDD3EC2AEF,
    ),
    "nistp521": (
        66,
        2**521 - 1,
        0x0051953EB9618E1C9A1F929A21A0B68540EEA2DA725B99B315F3B8B489918EF109E156193951EC7E937B1652C0BD3BB1BF073573DF883D2C34F1EF451FD46B503F00,
    ),
}


def _validate_blob(keytype: str, blob: bytes) -> None:
    reader = _WireReader(blob)
    if reader.string() != keytype.encode("ascii"):
        raise SSHKeyError("Key algorithm does not match its encoded blob")
    if "ed25519" in keytype:
        public = reader.string()
        if len(public) != 32 or public == bytes(32):
            raise SSHKeyError("Invalid Ed25519 public key length or value")
    elif keytype == "ssh-rsa":
        exponent, modulus = reader.positive_mpint(), reader.positive_mpint()
        if exponent < 3 or exponent % 2 == 0 or modulus.bit_length() < 1024 or modulus % 2 == 0 or exponent >= modulus:
            raise SSHKeyError("Invalid or too small RSA public key")
    else:
        curve = reader.string().decode("ascii", "replace")
        expected = "nistp256" if keytype.startswith("sk-") else keytype.removeprefix("ecdsa-sha2-")
        if curve != expected or curve not in _CURVES:
            raise SSHKeyError("ECDSA algorithm and curve do not match")
        size, prime, b = _CURVES[curve]
        point = reader.string()
        if len(point) != 1 + 2 * size or point[0] != 4:
            raise SSHKeyError("Invalid ECDSA point encoding")
        x, y = int.from_bytes(point[1:1 + size], "big"), int.from_bytes(point[1 + size:], "big")
        if x >= prime or y >= prime or (y * y - (x**3 - 3 * x + b)) % prime:
            raise SSHKeyError("ECDSA point is not on the selected curve")
    if keytype.startswith("sk-"):
        application = reader.string()
        if not application or b"\0" in application:
            raise SSHKeyError("Invalid security key application")
    if reader.pos != len(blob):
        raise SSHKeyError("Trailing data in SSH public key blob")


def parse_authorized_key(line: str) -> AuthorizedKey:
    """Parse one line, not a whole .pub file; callers remove its final newline.

    Identity excludes options/comments. Options represent effective restrictions
    (case insensitive flags, flag order, quoted values and repeated permissions).
    Unsupported key algorithms and options fail closed rather than being guessed.
    """
    if not isinstance(line, str) or any(char in line for char in "\r\n\0"):
        raise SSHKeyError("Expected exactly one public key line without a newline")
    if "PRIVATE KEY" in line.upper():
        raise SSHKeyError("Private key input is forbidden; supply only a public key")
    if any(ord(char) < 32 and char != "\t" for char in line) or len(line.encode("utf-8", "surrogateescape")) > 8192:
        raise SSHKeyError("Invalid control character or oversized public key line")
    line = line.strip(" \t")
    first, pos = _field(line)
    if first in _KEY_TYPES:
        keytype, options = first, ()
    else:
        options = _options(first)
        keytype, pos = _field(line, pos)
    if keytype not in _KEY_TYPES:
        raise SSHKeyError("Unsupported or missing SSH public key algorithm")
    encoded, pos = _field(line, pos)
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encoded):
        raise SSHKeyError("Malformed public key base64")
    try:
        blob = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SSHKeyError("Malformed public key base64") from exc
    canonical = base64.b64encode(blob).decode("ascii")
    if encoded not in {canonical, canonical.rstrip("=")}:
        raise SSHKeyError("Noncanonical public key base64")
    _validate_blob(keytype, blob)
    return AuthorizedKey(keytype, blob, options, line[pos:].lstrip(" \t"), line)


def _require_posix() -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise SSHSafetyError("Host SSH mutation requires POSIX ownership, dir_fd and no-follow support")


def _account(name: str, lookup: Callable | None) -> dict[str, Any]:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.@$-]+", name):
        raise SSHSafetyError("Invalid target account name")
    if lookup is None:
        import pwd
        lookup = pwd.getpwnam
    try:
        record = lookup(name)
    except KeyError as exc:
        raise SSHSafetyError("Target account does not exist") from exc
    if record.pw_name != name or record.pw_uid < 0 or record.pw_gid < 0:
        raise SSHSafetyError("Inconsistent account lookup result")
    home = _absolute_path(record.pw_dir)
    return {"name": name, "home": home, "uid": int(record.pw_uid), "gid": int(record.pw_gid)}


def _context(account: str, context: Mapping[str, Any]) -> dict[str, str]:
    allowed = {"user", "addr", "host", "laddr", "lport", "rdomain"}
    if not isinstance(context, Mapping) or set(context) - allowed:
        raise SSHSafetyError("Unsupported SSH connection context field")
    result = {key: str(value) for key, value in context.items()}
    if result.get("user", account) != account:
        raise SSHSafetyError("Connection context user does not match the target account")
    result["user"] = account
    if not {"addr", "host", "laddr", "lport"} <= result.keys():
        raise SSHSafetyError("Connection context requires addr, host, laddr and lport for Match evaluation")
    if any(not value or any(char.isspace() or char in ",=\0" for char in value) for value in result.values()):
        raise SSHSafetyError("Invalid SSH connection context value")
    try:
        ipaddress.ip_address(result["addr"])
        ipaddress.ip_address(result["laddr"])
        if not result["lport"].isascii() or not result["lport"].isdigit() or not 1 <= int(result["lport"]) <= 65535:
            raise ValueError
    except ValueError as exc:
        raise SSHSafetyError("Invalid SSH connection IP address or local port") from exc
    return result


def _run(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SSHSafetyError("sshd effective configuration check failed") from exc
    return result.stdout


def _effective(context: dict[str, str], runner: Callable | None, sshd_path: str) -> tuple[dict[str, str], str]:
    if not os.path.isabs(sshd_path):
        raise SSHSafetyError("sshd executable path must be absolute")
    command = [sshd_path, "-T", "-C", ",".join(key + "=" + context[key] for key in sorted(context))]
    try:
        result = (runner or _run)(command)
        if hasattr(result, "returncode") and result.returncode != 0:
            raise SSHSafetyError("sshd effective configuration check failed")
        output = result if isinstance(result, str) else result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SSHSafetyError("sshd effective configuration check failed") from exc
    if not isinstance(output, str) or not output.strip():
        raise SSHSafetyError("sshd returned no effective configuration")
    config: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"authorizedkeysfile", "authorizedkeyscommand", "pubkeyauthentication"}:
            key = parts[0].lower()
            if key in config:
                raise SSHSafetyError("Ambiguous effective SSH configuration")
            config[key] = parts[1]
    if config.get("pubkeyauthentication", "yes").lower() != "yes":
        raise SSHSafetyError("Public key authentication is disabled for this connection")
    return config, hashlib.sha256(output.encode("utf-8")).hexdigest()


def _absolute_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or path == "/":
        raise SSHSafetyError("Expected a non-root absolute POSIX path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path) or ".." in path.split("/"):
        raise SSHSafetyError("Unsafe parent traversal or control character in path")
    return str(PurePosixPath(path))


def _expand_path(template: str, account: dict[str, Any]) -> str:
    if not isinstance(template, str) or not template:
        raise SSHSafetyError("Empty authorized keys path")
    replacements = {"h": account["home"], "u": account["name"], "U": str(account["uid"]), "%": "%"}
    parts, pos = [], 0
    while pos < len(template):
        if template[pos] == "%":
            pos += 1
            if pos == len(template) or template[pos] not in replacements:
                raise SSHSafetyError("Unsupported AuthorizedKeysFile percent token")
            parts.append(replacements[template[pos]])
        else:
            parts.append(template[pos])
        pos += 1
    expanded = "".join(parts)
    if not expanded.startswith("/"):
        expanded = account["home"] + "/" + expanded
    return _absolute_path(expanded)


def _paths(config: dict[str, str], account: dict[str, Any]) -> list[str]:
    if "authorizedkeysfile" not in config:
        raise SSHSafetyError("Effective AuthorizedKeysFile is absent; no fallback path is assumed")
    try:
        templates = shlex.split(config["authorizedkeysfile"])
    except ValueError as exc:
        raise SSHSafetyError("Malformed effective AuthorizedKeysFile") from exc
    if not templates or any(item.lower() == "none" for item in templates):
        raise SSHSafetyError("Effective AuthorizedKeysFile is none; command-only authentication cannot be changed here")
    return list(dict.fromkeys(_expand_path(item, account) for item in templates))


def _stat_record(path: str, info: os.stat_result, *, directory: bool) -> dict[str, Any]:
    result = {"path": path, "exists": True, "device": info.st_dev, "inode": info.st_ino,
              "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
    if not directory:
        result.update(nlink=info.st_nlink, size=info.st_size, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
    return result


def _safe_stat(path: str, info: os.stat_result, account: dict[str, Any], *, directory: bool) -> dict[str, Any]:
    if stat.S_ISLNK(info.st_mode):
        raise SSHSafetyError("Symlink is forbidden in authorized keys path: " + path)
    if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise SSHSafetyError("Unexpected file type in authorized keys path: " + path)
    if info.st_uid not in {0, account["uid"]} or info.st_mode & 0o022:
        raise SSHSafetyError("Unsafe owner or writable permissions: " + path)
    if info.st_mode & 0o7000 or (not directory and (info.st_mode & 0o111 or info.st_nlink != 1)):
        raise SSHSafetyError("Unsafe special mode, executable key file or hard link: " + path)
    return _stat_record(path, info, directory=directory)


def _open_directory(path: str, account: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """Walk without following links; caller owns the returned directory fd."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    snapshots = [_safe_stat("/", os.fstat(descriptor), account, directory=True)]
    prefix = ""
    try:
        for component in PurePosixPath(path).parts[1:]:
            prefix += "/" + component
            info = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            before = _safe_stat(prefix, info, account, directory=True)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            if before != _safe_stat(prefix, os.fstat(child), account, directory=True):
                os.close(child)
                raise SSHSafetyError("Directory changed while opening: " + prefix)
            os.close(descriptor)
            descriptor = child
            snapshots.append(before)
        return descriptor, snapshots
    except BaseException:
        os.close(descriptor)
        raise


def _inspect(path: str, account: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    path = _absolute_path(path)
    components = PurePosixPath(path).parts[1:]
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directories = [_safe_stat("/", os.fstat(descriptor), account, directory=True)]
    prefix, missing = "", False
    try:
        for component in components[:-1]:
            prefix += "/" + component
            if not missing:
                try:
                    info = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    missing = True
                else:
                    before = _safe_stat(prefix, info, account, directory=True)
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    if before != _safe_stat(prefix, os.fstat(child), account, directory=True):
                        os.close(child)
                        raise SSHSafetyError("Directory changed while reading: " + prefix)
                    os.close(descriptor)
                    descriptor = child
                    directories.append(before)
            if missing:
                directories.append({"path": prefix, "exists": False})
        snapshot = {"path": path, "directories": directories, "file": {"path": path, "exists": False}, "sha256": None}
        if missing:
            return snapshot, b""
        try:
            info = os.stat(components[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return snapshot, b""
        before = _safe_stat(path, info, account, directory=False)
        file_fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            if before != _safe_stat(path, os.fstat(file_fd), account, directory=False):
                raise SSHSafetyError("Authorized keys file changed while opening")
            chunks, total = [], 0
            while chunk := os.read(file_fd, 65536):
                total += len(chunk)
                if total > _MAX_FILE_BYTES:
                    raise SSHSafetyError("Authorized keys file exceeds the safe scan limit")
                chunks.append(chunk)
            if before != _safe_stat(path, os.fstat(file_fd), account, directory=False):
                raise SSHSafetyError("Authorized keys file changed while reading")
        finally:
            os.close(file_fd)
        if before != _safe_stat(path, os.stat(components[-1], dir_fd=descriptor, follow_symlinks=False), account, directory=False):
            raise SSHSafetyError("Authorized keys file was replaced while reading")
        data = b"".join(chunks)
        snapshot.update(file=before, sha256=hashlib.sha256(data).hexdigest())
        return snapshot, data
    except OSError as exc:
        raise SSHSafetyError("Cannot safely inspect authorized keys path: " + path) from exc
    finally:
        os.close(descriptor)


def _scan(data: bytes, wanted: AuthorizedKey, path: str) -> list[dict[str, Any]]:
    matches = []
    for number, raw in enumerate(data.split(b"\n"), 1):
        raw = raw.removesuffix(b"\r")
        if not raw.strip() or raw.lstrip(b" \t").startswith(b"#"):
            continue
        try:
            existing = parse_authorized_key(raw.decode("utf-8", "surrogateescape"))
        except SSHKeyError as exc:
            raise SSHSafetyError(f"Cannot safely parse existing key at {path}:{number}; no changes made") from exc
        if existing.identity == wanted.identity:
            if existing.options != wanted.options:
                raise SSHSafetyError(f"Same public key has conflicting authorization restrictions at {path}:{number}")
            matches.append({"path": path, "line": number})
    return matches


def plan_append(
    account: str,
    key_text: str,
    connection_context: Mapping[str, Any],
    selected_path: str | None = None,
    *,
    runner: Callable | None = None,
    account_lookup: Callable | None = None,
    sshd_path: str = _SSHD,
) -> dict[str, Any]:
    """Read-only plan. ``runner(argv)`` returns str or CompletedProcess.

    ``account_lookup(name)`` defaults to pwd.getpwnam and is injectable for tests.
    Multiple distinct effective files always require an explicit selected_path.
    The account home and every ancestor/file must be safe, including unselected
    files. Existing unsupported/malformed active key lines fail closed.
    """
    wanted = parse_authorized_key(key_text)
    _require_posix()
    identity = _account(account, account_lookup)
    context = _context(account, connection_context)
    config, config_hash = _effective(context, runner, sshd_path)
    paths = _paths(config, identity)
    if selected_path is None:
        if len(paths) != 1:
            raise SSHSafetyError("Multiple effective AuthorizedKeysFile paths; explicitly select one: " + ", ".join(paths))
        target = paths[0]
    else:
        # A serialized plan contains the already-expanded path. Match literal
        # paths first so a filename produced by %% is never expanded twice.
        if not isinstance(selected_path, str) or not selected_path:
            raise SSHSafetyError("Selected AuthorizedKeysFile path must be a non-empty string")
        literal = _absolute_path(selected_path if selected_path.startswith("/") else identity["home"] + "/" + selected_path)
        target = literal if literal in paths else _expand_path(selected_path, identity)
        if target not in paths:
            raise SSHSafetyError("Selected path is not an effective AuthorizedKeysFile")
    try:
        home_fd, home_snapshot = _open_directory(identity["home"], identity)
    except OSError as exc:
        raise SSHSafetyError("Account home does not exist or cannot be inspected safely") from exc
    os.close(home_fd)
    snapshots, matches = [], []
    for path in paths:
        snapshot, data = _inspect(path, identity)
        if path == target and any(not item["exists"] and not item["path"].startswith(identity["home"] + "/")
                                  for item in snapshot["directories"]):
            raise SSHSafetyError("Missing AuthorizedKeysFile parent directories outside the account home must be provisioned separately")
        snapshots.append(snapshot)
        matches.extend(_scan(data, wanted, path))
    return {
        "version": 1,
        "kind": "ssh-key-append",
        "status": "noop" if matches else "action",
        "account": identity,
        "connection_context": context,
        "effective_config": {"sha256": config_hash, "sshd_path": sshd_path, "authorized_keys_files": paths,
                             "authorized_keys_command": config.get("authorizedkeyscommand", "none")},
        "key": {"line": wanted.line, "fingerprint": wanted.fingerprint,
                "options": [list(option) for option in wanted.options]},
        "fingerprint": wanted.fingerprint,
        "target": target,
        "hashes": {snapshot["path"]: snapshot["sha256"] for snapshot in snapshots},
        "snapshots": snapshots,
        "home_snapshot": home_snapshot,
        "matches": matches,
        "verification": "unverified",
    }


def _validate(plan: dict[str, Any], runner: Callable | None, lookup: Callable | None, sshd_path: str) -> None:
    try:
        if plan["version"] != 1 or plan["kind"] != "ssh-key-append":
            raise SSHSafetyError("Unsupported SSH append plan")
        current = plan_append(plan["account"]["name"], plan["key"]["line"], plan["connection_context"], plan["target"],
                              runner=runner, account_lookup=lookup, sshd_path=sshd_path)
    except (KeyError, TypeError) as exc:
        raise SSHSafetyError("Invalid SSH append plan") from exc
    if current != plan:
        raise SSHSafetyError("SSH plan is stale or modified: account, config, file hash or directory state changed; re-plan")


def _write_file(parent_fd: int, name: str, data: bytes, owner: int, group: int, mode: int) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SSHSafetyError("Short write while preparing authorized keys")
            view = view[written:]
        current = os.fstat(descriptor)
        if (current.st_uid, current.st_gid) != (owner, group):
            os.fchown(descriptor, owner, group)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_parents(plan: dict[str, Any]) -> tuple[int, dict[str, dict[str, Any]]]:
    identity, target = plan["account"], plan["target"]
    snapshot = next(item for item in plan["snapshots"] if item["path"] == target)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created: dict[str, dict[str, Any]] = {}
    try:
        for expected in snapshot["directories"][1:]:
            name = PurePosixPath(expected["path"]).name
            if not expected["exists"]:
                # mkdir is exclusive: a concurrent creator causes a safe failure.
                os.mkdir(name, 0o700, dir_fd=descriptor)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                if not expected["exists"]:
                    info = os.fstat(child)
                    if (info.st_uid, info.st_gid) != (identity["uid"], identity["gid"]):
                        os.fchown(child, identity["uid"], identity["gid"])
                    os.fchmod(child, 0o700)
                    os.fsync(child)
                    os.fsync(descriptor)
                actual = _safe_stat(expected["path"], os.fstat(child), identity, directory=True)
                if expected["exists"] and actual != expected:
                    raise SSHSafetyError("Directory changed before append")
                if not expected["exists"]:
                    created[expected["path"]] = actual
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor, created
    except BaseException:
        os.close(descriptor)
        raise


def apply_append(
    plan: dict[str, Any],
    *,
    runner: Callable | None = None,
    account_lookup: Callable | None = None,
    sshd_path: str = _SSHD,
) -> dict[str, Any]:
    """Revalidate and append atomically; never disable or restart SSH.

    The executable is supplied independently of the serialized plan to avoid
    executing an arbitrary plan-controlled command. Returns written/unverified or
    noop/unverified. A successful write alone does not verify SSH login.
    """
    _require_posix()
    import fcntl

    _validate(plan, runner, account_lookup, sshd_path)
    locked: list[int] = []
    parent_fd: int | None = None
    created_parent_locked = False
    temporary: str | None = None
    backup: str | None = None
    try:
        # Lock all existing parents, including unselected files; sort to avoid
        # lock ordering inversions. For a missing .ssh, lock its nearest parent.
        lock_paths = sorted({next(item["path"] for item in reversed(snapshot["directories"]) if item["exists"])
                             for snapshot in plan["snapshots"]})
        for path in lock_paths:
            descriptor, _ = _open_directory(path, plan["account"])
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(descriptor)
                raise SSHSafetyError("Another authorized keys update holds the directory lock") from exc
            locked.append(descriptor)
        _validate(plan, runner, account_lookup, sshd_path)
        if plan["status"] == "noop":
            return {"status": "noop", "verification": "unverified", "fingerprint": plan["fingerprint"],
                    "target": plan["target"], "backup": None, "matches": plan["matches"]}
        expected = copy.deepcopy(plan)
        parent_fd, created = _create_parents(expected)
        if str(PurePosixPath(expected["target"]).parent) in created:
            try:
                fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SSHSafetyError("Another update holds the newly created directory lock") from exc
            created_parent_locked = True
        for snapshot in expected["snapshots"]:
            snapshot["directories"] = [created.get(item["path"], item) for item in snapshot["directories"]]
        _validate(expected, runner, account_lookup, sshd_path)
        target = expected["target"]
        snapshot, before = _inspect(target, expected["account"])
        if snapshot != next(item for item in expected["snapshots"] if item["path"] == target):
            raise SSHSafetyError("Authorized keys changed before backup")
        addition = (b"\n" if before and not before.endswith(b"\n") else b"") + expected["key"]["line"].encode("utf-8") + b"\n"
        after = before + addition
        name = PurePosixPath(target).name
        suffix = uuid.uuid4().hex
        owner = snapshot["file"].get("uid", expected["account"]["uid"])
        group = snapshot["file"].get("gid", expected["account"]["gid"])
        mode = snapshot["file"].get("mode", 0o600)
        if snapshot["file"]["exists"]:
            backup_name = "." + name + ".proxyhub." + suffix + ".bak"
            _write_file(parent_fd, backup_name, before, owner, group, mode & 0o600)
            os.fsync(parent_fd)
            backup = str(PurePosixPath(target).parent / backup_name)
        temporary = "." + name + ".proxyhub." + suffix + ".tmp"
        _write_file(parent_fd, temporary, after, owner, group, mode)
        # Includes every effective file and the complete effective sshd output.
        _validate(expected, runner, account_lookup, sshd_path)
        parent_state = _safe_stat(str(PurePosixPath(target).parent), os.fstat(parent_fd), expected["account"], directory=True)
        if parent_state != snapshot["directories"][-1]:
            raise SSHSafetyError("Target parent changed before replacement")
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = None
        os.fsync(parent_fd)
        result_snapshot, actual = _inspect(target, expected["account"])
        if actual != after or (result_snapshot["file"]["uid"], result_snapshot["file"]["gid"], result_snapshot["file"]["mode"]) != (owner, group, mode):
            raise SSHSafetyError("Post-write verification failed; inspect target and backup before retrying")
        return {"status": "written", "verification": "unverified", "fingerprint": expected["fingerprint"],
                "target": target, "backup": backup, "before_sha256": snapshot["sha256"],
                "after_sha256": result_snapshot["sha256"], "created_directories": list(created)}
    except OSError as exc:
        raise SSHSafetyError("Authorized keys append could not complete safely; re-plan before retrying") from exc
    finally:
        if temporary is not None and parent_fd is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        if parent_fd is not None:
            if created_parent_locked:
                fcntl.flock(parent_fd, fcntl.LOCK_UN)
            os.close(parent_fd)
        for descriptor in reversed(locked):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
