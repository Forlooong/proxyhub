"""ACME bootstrap and versioned certificate imports; private keys stay on host."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile

from .core import OperationError, atomic_write, canonical, run
from .proxy import validate_domain
from .site import validate_certificate

ROOT = Path('/var/lib/proxyhub')
_MAX_PEM_BYTES = 1024 * 1024
_PEM = re.compile(
    br'-----BEGIN ([A-Z ]+)-----\r?\n([A-Za-z0-9+/=\r\n]+)-----END \1-----'
)


def managed_acme(server: dict) -> bool:
    """Only explicitly issued ProxyHub lineages opt into automatic renewal."""
    marker = Path(server['certificate_path']).parent/'acme.json'
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        source = json.loads(marker.read_text(encoding='utf-8'))
        return (source['source'] == 'acme-http-01' and source['lineage'] == 'proxyhub'
                and set(source['domains']) == {server['site_domain'],server['subscription_domain']})
    except (KeyError, TypeError, ValueError):
        return False


def _read_pem(path: Path) -> bytes:
    with Path(path).open('rb') as source:
        data = source.read(_MAX_PEM_BYTES + 1)
    if not data or len(data) > _MAX_PEM_BYTES:
        raise OperationError('Certificate and key inputs must contain at most 1 MiB each')
    return data


def _pem_objects(data: bytes, allowed: set[bytes], maximum: int) -> list[bytes]:
    """Reject ignored tails, additional private keys and malformed PEM blocks."""
    objects, position = [], 0
    for match in _PEM.finditer(data):
        if data[position:match.start()].strip() or match[1] not in allowed:
            raise OperationError('Unexpected content in certificate or private key PEM')
        try:
            decoded = base64.b64decode(b''.join(match[2].splitlines()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise OperationError('Invalid certificate or private key PEM encoding') from exc
        if not decoded or len(objects) >= maximum:
            raise OperationError('Invalid certificate or private key PEM object count')
        objects.append(decoded)
        position = match.end()
    if not objects or data[position:].strip():
        raise OperationError('Unexpected content in certificate or private key PEM')
    return objects


def _validate_complete_pem(certificate: bytes, key: bytes) -> None:
    # x509/pkey normally consume only the first PEM object. Inspect every
    # certificate, including DER round-trip equality to reject ignored tails.
    objects = _pem_objects(certificate, {b'CERTIFICATE'}, 16)
    _pem_objects(key, {b'PRIVATE KEY', b'RSA PRIVATE KEY', b'EC PRIVATE KEY'}, 1)
    for encoded in objects:
        base64_text = base64.b64encode(encoded)
        pem = (b'-----BEGIN CERTIFICATE-----\n'
               + b'\n'.join(base64_text[index:index + 64] for index in range(0, len(base64_text), 64))
               + b'\n-----END CERTIFICATE-----\n')
        try:
            result = subprocess.run(
                ['openssl', 'x509', '-inform', 'PEM', '-outform', 'DER'],
                input=pem, capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OperationError('Complete certificate PEM validation could not finish') from exc
        if result.returncode or result.stdout != encoded:
            raise OperationError('Invalid certificate in fullchain PEM')


def _check_existing(target: Path, certificate: bytes, key: bytes) -> None:
    if target.is_symlink() or not target.is_dir():
        raise OperationError('Existing certificate revision is not a managed directory')
    for name, expected in (('fullchain.pem', certificate), ('privkey.pem', key)):
        path = target / name
        if path.is_symlink() or not path.is_file() or _read_pem(path) != expected:
            raise OperationError('Existing certificate revision differs; refusing to overwrite it')


def _sync_directory(path: Path) -> None:
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def import_pair(certificate: Path, key: Path, domains: list[str]) -> dict:
    """Validate a private snapshot, then publish an immutable pair directory.

    The revision binds the complete certificate chain and key bytes. A second
    import of the same leaf with a different chain gets a new path; existing
    deployment revisions are never changed by an import. Trust-chain policy is
    separate from these complete-PEM and local key/SAN/date checks.
    """
    certificate_bytes, key_bytes = _read_pem(certificate), _read_pem(key)
    parent = ROOT/'certificates'
    if parent.is_symlink():
        raise OperationError('Certificate storage must not be a symlink')
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.import-', dir=parent))
    try:
        atomic_write(staging/'fullchain.pem', certificate_bytes, 0o644)
        atomic_write(staging/'privkey.pem', key_bytes, 0o600)
        _validate_complete_pem(certificate_bytes, key_bytes)
        details = validate_certificate(staging/'fullchain.pem', staging/'privkey.pem', domains)
        digest = hashlib.sha256(b'proxyhub-certificate-pair-v1\0')
        for data in (certificate_bytes, key_bytes):
            digest.update(len(data).to_bytes(8, 'big'))
            digest.update(data)
        target = parent/digest.hexdigest()
        if target.exists() or target.is_symlink():
            _check_existing(target, certificate_bytes, key_bytes)
        else:
            _sync_directory(staging)
            try:
                # Same-filesystem directory rename publishes both files at once.
                # Concurrent identical imports may have won the destination.
                os.rename(staging, target)
            except OSError:
                if not target.exists() and not target.is_symlink():
                    raise
                _check_existing(target, certificate_bytes, key_bytes)
            _sync_directory(parent)
        return {**details, 'certificate_path': str(target/'fullchain.pem'), 'key_path': str(target/'privkey.pem')}
    finally:
        if staging.exists():
            if staging.resolve().parent != parent.resolve() or not staging.name.startswith('.import-'):
                raise OperationError('Unsafe certificate staging cleanup path')
            shutil.rmtree(staging)


def issue(domains: list[str], *, standalone: bool = True) -> dict:
    domains = list(dict.fromkeys(validate_domain(domain) for domain in domains))
    if not 1 <= len(domains) <= 10:
        raise OperationError('Provide 1–10 domain names')
    if standalone:
        for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
            with socket.socket(family, socket.SOCK_STREAM) as test:
                if family == socket.AF_INET6:
                    test.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    test.bind((address, 80))
                except OSError as exc:
                    raise OperationError('HTTP-01 bootstrap needs TCP 80 free; use the managed webroot for renewal') from exc
    acme = ROOT/'acme'
    for name in ('config', 'work', 'logs'):
        (acme/name).mkdir(parents=True, mode=0o700, exist_ok=True)
    arguments = ['certbot', 'certonly', '--non-interactive', '--agree-tos',
                 '--register-unsafely-without-email', '--preferred-challenges', 'http',
                 '--cert-name', 'proxyhub', '--config-dir', str(acme/'config'),
                 '--work-dir', str(acme/'work'), '--logs-dir', str(acme/'logs'),
                 '--keep-until-expiring']
    arguments += ['--standalone'] if standalone else ['--webroot', '-w', str(acme)]
    for domain in domains:
        arguments += ['-d', domain]
    run(arguments, timeout=180)
    source = acme/'config/live/proxyhub'
    result = import_pair(source/'fullchain.pem', source/'privkey.pem', domains)
    atomic_write(Path(result['certificate_path']).parent/'acme.json',
                 canonical({'source':'acme-http-01','lineage':'proxyhub','domains':domains}))
    result['source'] = 'acme-http-01'
    result['consumer_reload'] = 'requires_deployment_apply'
    return result
