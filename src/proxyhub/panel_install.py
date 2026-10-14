"""Install only the namespaced panel account, credential and two management services."""
from __future__ import annotations

import hashlib
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat

from .core import OperationError, atomic_write, canonical, require_root, run
from .rpc import decode_json

PREFIX = Path("/opt/proxyhub")
PANEL = Path("/var/lib/proxyhub/panel")
UNIT_DIR = Path("/etc/systemd/system")
RUNTIME = Path("/run/proxyhub")
ACCOUNT = "proxyhub-panel"
MARKER = "# Managed by ProxyHub panel\n"
UNIT_NAMES = ("proxyhub-manager.service", "proxyhub-panel.service")


def _direct(path: Path, *, directory: bool = False) -> None:
    if path.is_symlink():
        raise OperationError("Panel paths must not be symlinks")
    if not path.exists():
        return
    info = path.stat()
    if directory != stat.S_ISDIR(info.st_mode):
        raise OperationError("A managed panel path has an unexpected type")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise OperationError("Managed panel files must be regular files without hard links")
    if os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o022):
        raise OperationError("Panel paths must be root owned and not writable by others")


def _json(path: Path) -> dict:
    _direct(path)
    with path.open("rb") as source:
        value = decode_json(source.read(8193), 8192)
    if not isinstance(value, dict):
        raise OperationError("Invalid panel ownership/configuration record")
    return value


def _identity() -> tuple[object | None, object | None]:
    import grp
    import pwd
    try:
        user = pwd.getpwnam(ACCOUNT)
    except KeyError:
        user = None
    try:
        group = grp.getgrnam(ACCOUNT)
    except KeyError:
        group = None
    return user, group


def _check_identity(user: object, group: object, owner: dict) -> None:
    if (user is None or group is None or user.pw_uid <= 0 or group.gr_gid <= 0
            or user.pw_uid != owner["uid"] or user.pw_gid != owner["gid"]
            or group.gr_gid != owner["gid"] or user.pw_dir != "/nonexistent"
            or user.pw_shell != "/usr/sbin/nologin" or user.pw_gecos != "ProxyHub management gateway"
            or set(group.gr_mem) - {ACCOUNT}):
        raise OperationError("Existing panel account does not match its recorded identity")
    if os.name == "posix" and set(os.getgrouplist(ACCOUNT, user.pw_gid)) != {user.pw_gid}:
        raise OperationError("The panel account has unexpected supplementary groups")


def _ownership() -> dict:
    if not (PANEL / "ownership.json").exists():
        raise OperationError("An existing panel directory has no ProxyHub ownership record")
    owner = _json(PANEL / "ownership.json")
    if (set(owner) != {"component", "version", "uid", "gid"}
            or owner["component"] != "proxyhub-panel" or type(owner["version"]) is not int or owner["version"] != 1
            or type(owner["uid"]) is not int or type(owner["gid"]) is not int):
        raise OperationError("Panel ownership record is invalid")
    return owner


def _credential() -> str:
    path = PANEL / "credential"
    _direct(path)
    with path.open("r", encoding="ascii") as source:
        value = source.read(256)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}\n", value):
        raise OperationError("The stored panel credential is invalid")
    if os.name == "posix" and (path.stat().st_mode & 0o777) != 0o600:
        raise OperationError("The panel credential must have root-only permissions")
    return value[:-1]


def _context(value: str | None) -> dict | None:
    if not value:
        return None
    fields = value.split()
    if len(fields) != 4:
        raise OperationError("SSH_CONNECTION has an invalid shape")
    try:
        client, server = str(ipaddress.ip_address(fields[0])), str(ipaddress.ip_address(fields[2]))
        if not all(field.isascii() and field.isdigit() and 1 <= int(field) <= 65535 for field in (fields[1], fields[3])):
            raise ValueError()
    except ValueError as exc:
        raise OperationError("SSH_CONNECTION contains an invalid address or port") from exc
    return {"addr": client, "host": client, "laddr": server, "lport": int(fields[3])}


def units(uid: int, gid: int) -> dict[str, str]:
    common = ("Restart=on-failure\nRestartSec=2\nUMask=0077\nNoNewPrivileges=yes\n"
              "PrivateTmp=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\n"
              "ProtectControlGroups=yes\nRestrictRealtime=yes\nLockPersonality=yes\n"
              "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK\n"
              "StandardOutput=null\nStandardError=journal\n")
    manager = (MARKER + "[Unit]\nDescription=ProxyHub privileged management operations\n"
        "After=network-online.target\nWants=network-online.target\n\n[Service]\n"
        "User=root\nGroup=root\nRuntimeDirectory=proxyhub\nRuntimeDirectoryMode=0755\n"
        "RuntimeDirectoryPreserve=yes\nEnvironment=PYTHONPATH=/opt/proxyhub/app/src\n"
        f"ExecStart=/usr/bin/python3 -m proxyhub.manager --gateway-uid {uid} --socket-gid {gid}\n"
        "ProtectSystem=full\nReadOnlyPaths=/opt/proxyhub\nReadWritePaths=/etc/ssh /etc/systemd/system /var/lib/proxyhub /run/proxyhub\n"
        + common + "\n[Install]\nWantedBy=multi-user.target\n")
    gateway = (MARKER + "[Unit]\nDescription=ProxyHub loopback management panel\n"
        "After=proxyhub-manager.service\nRequires=proxyhub-manager.service\n\n[Service]\n"
        f"User={ACCOUNT}\nGroup={ACCOUNT}\n"
        "Environment=PYTHONPATH=/opt/proxyhub/app/src:/opt/proxyhub/panel-python\n"
        "ExecStart=/usr/bin/python3 -m proxyhub.gateway --config /var/lib/proxyhub/panel/gateway.json\n"
        "ProtectHome=yes\nProtectSystem=strict\nCapabilityBoundingSet=\n"
        "InaccessiblePaths=/var/lib/proxyhub/panel/credential /var/lib/proxyhub/panel/ownership.json "
        "-/var/lib/proxyhub/panel/ssh-context.json /var/lib/proxyhub/plans /var/lib/proxyhub/certificates "
        "/var/lib/proxyhub/revisions /var/lib/proxyhub/acme /var/lib/proxyhub/publish\n"
        + common + "\n[Install]\nWantedBy=multi-user.target\n")
    return dict(zip(UNIT_NAMES, (manager, gateway), strict=True))


def install() -> dict:
    require_root()
    _direct(PREFIX, directory=True)
    _direct(PREFIX / ".proxyhub-owned")
    if not (PREFIX / ".proxyhub-owned").is_file():
        raise OperationError("Install the pinned ProxyHub runtimes first")
    assets = PREFIX / "app/packages/panel/dist"
    _direct(assets, directory=True)
    _direct(assets / "index.html")
    if not (assets / "index.html").is_file():
        raise OperationError("Build and copy the panel assets before installing services")
    for module in ("fastapi", "uvicorn"):
        if not (PREFIX / "panel-python" / module / "__init__.py").is_file():
            raise OperationError("Install the pinned panel Python dependencies first")
    _direct(PANEL.parent, directory=True)
    _direct(PANEL, directory=True)
    _direct(UNIT_DIR, directory=True)
    _direct(RUNTIME, directory=True)
    context = _context(os.environ.get("SSH_CONNECTION"))
    owned = PANEL.exists()
    owner = _ownership() if owned else None
    user, group = _identity()
    if owner:
        _check_identity(user, group, owner)
    elif user is not None or group is not None:
        raise OperationError("An existing panel user or group is not owned by this installation")
    for name in UNIT_NAMES:
        destination = UNIT_DIR / name
        _direct(destination)
        if destination.exists():
            with destination.open("r", encoding="utf-8") as source:
                header = source.read(len(MARKER))
            if not owned or header != MARKER:
                raise OperationError("An existing panel service unit is not owned by ProxyHub")
    for name in ("credential", "gateway.json", "ssh-context.json"):
        _direct(PANEL / name)
    credential = _credential() if owned else secrets.token_urlsafe(32)
    if owned and (PANEL / "gateway.json").exists():
        saved = _json(PANEL / "gateway.json")
        if saved.get("credential_sha256") != hashlib.sha256(credential.encode()).hexdigest():
            raise OperationError("The gateway digest does not match the saved panel credential")
    if not owned:
        run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
             "--shell", "/usr/sbin/nologin", "--comment", "ProxyHub management gateway", ACCOUNT])
        user, group = _identity()
        if user is None or group is None:
            raise OperationError("The panel service account was not created")
        owner = {"component": "proxyhub-panel", "version": 1, "uid": user.pw_uid, "gid": group.gr_gid}
        _check_identity(user, group, owner)
        PANEL.mkdir(mode=0o700)
        atomic_write(PANEL / "ownership.json", canonical(owner), 0o600)
        atomic_write(PANEL / "credential", (credential + "\n").encode(), 0o600)
    os.chown(PANEL, 0, owner["gid"])
    PANEL.chmod(0o750)
    config = {"credential_sha256": hashlib.sha256(credential.encode()).hexdigest(), "port": 18765,
              "session_ttl_seconds": 1800, "assets_path": str(assets)}
    atomic_write(PANEL / "gateway.json", canonical(config), 0o640)
    os.chown(PANEL / "gateway.json", 0, owner["gid"])
    if context:
        atomic_write(PANEL / "ssh-context.json", canonical(context), 0o600)
    RUNTIME.mkdir(mode=0o755, exist_ok=True)
    for name, content in units(owner["uid"], owner["gid"]).items():
        atomic_write(UNIT_DIR / name, content.encode(), 0o644)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", *UNIT_NAMES])
    run(["systemctl", "restart", *UNIT_NAMES])
    for name in UNIT_NAMES:
        run(["systemctl", "is-active", "--quiet", name])
    return {"installed": list(UNIT_NAMES), "url": "http://127.0.0.1:18765", "loopback_only": True,
            "credential_created": not owned, "credential_command": "proxyhub panel access",
            "ssh_context_captured": context is not None}


def access() -> dict:
    """Explicit root-only credential disclosure; never called by the gateway."""
    require_root()
    _direct(PANEL, directory=True)
    owner = _ownership()
    _check_identity(*_identity(), owner)
    context_path = PANEL / "ssh-context.json"
    host, port = "<VPS>", "<SSH_PORT>"
    if context_path.exists():
        context = _json(context_path)
        try:
            address = ipaddress.ip_address(context["laddr"])
            port_number = context["lport"]
            if type(port_number) is not int or not 1 <= port_number <= 65535:
                raise ValueError()
        except (KeyError, ValueError) as exc:
            raise OperationError("The stored SSH context is invalid") from exc
        host = f"[{address}]" if address.version == 6 else str(address)
        port = str(port_number)
    return {"url": "http://127.0.0.1:18765", "credential": _credential(),
            "ssh_tunnel": f"ssh -N -L 127.0.0.1:18765:127.0.0.1:18765 -p {port} root@{host}",
            "instructions": "Run the SSH tunnel on your computer, open the local URL, and enter the credential.",
            "session_ttl_seconds": 1800}
