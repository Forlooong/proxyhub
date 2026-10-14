"""Small shared primitives; no implicit privileged operations at import time."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


class OperationError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    if path.is_symlink():
        raise OperationError("Refusing a symlink destination")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".proxyhub-", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(argv: list[str], timeout: int = 30, *, input: str | None = None) -> str:
    result = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Child output may contain a URI, a UUID or a key; retain only a bounded
        # operation identity in the public error. Privileged diagnostics are separate.
        raise OperationError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
    return result.stdout


def require_root() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise OperationError("This operation requires Linux root; help and render tests do not")
