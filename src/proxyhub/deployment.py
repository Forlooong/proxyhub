"""Single-node immutable candidates, explicit activation and rollback."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import time

from .core import OperationError, atomic_write, canonical, digest, run
from .proxy import validate_spec, render_sing_box
from .site import render_nginx, render_site, validate_certificate
from .publication import build_bundle, save_bundle
from .state import StateStore

ROOT = Path('/var/lib/proxyhub')
BIN = Path('/opt/proxyhub/bin')
APP = Path('/opt/proxyhub/app')
CANDIDATE_FILES = ('sing-box.json', 'nginx.conf', 'site/index.html', 'bundle.json', 'config.json', 'client.yaml')


def client_config(server: dict, overrides: dict) -> dict:
    node = {key: server[key] for key in ('uuid', 'public_key', 'short_id')}
    node.update(id='local-reality', name=server['node_name'], server=server['public_address'],
                port=server['public_port'], server_name=server['site_domain'])
    text = run([str(BIN/'node'), str(APP/'packages/converter/dist/worker.js')], input=json.dumps({'node':node,'overrides':overrides}), timeout=20)
    return json.loads(text)


def prepare(store: StateStore, config: dict, *, expected_revision: int | None = None) -> dict:
    import pwd
    base_revision = store.revision if expected_revision is None else expected_revision
    if store.revision != base_revision:
        raise OperationError('Configuration changed before preparation')
    if store.pending():
        raise OperationError('Recover the interrupted activation before preparing a deployment')
    if set(config) != {'server', 'subscription', 'overrides'}:
        raise OperationError('Deployment requires server, subscription and overrides objects only')
    server = validate_spec(config['server'])
    if server['publisher_port'] != 18890:
        raise OperationError('The phase-1 publisher unit reserves loopback port 18890')
    certificate = validate_certificate(server['certificate_path'], server['key_path'], [server['site_domain'],server['subscription_domain']])
    generated = client_config(server, config['overrides'])
    revision = secrets.token_hex(16)
    directory = ROOT/'revisions'/revision
    directory.mkdir(parents=True, mode=0o711)
    (directory/'site').mkdir(mode=0o755)
    proxy_gid = pwd.getpwnam('proxyhub-proxy').pw_gid
    singbox = directory/'sing-box.json'
    atomic_write(singbox, canonical(render_sing_box(server)), 0o640)
    os.chown(singbox, 0, proxy_gid)
    atomic_write(directory/'nginx.conf', render_nginx(server, str(directory)).encode(), 0o600)
    atomic_write(directory/'site/index.html', render_site(server).encode(), 0o644)
    subscription = dict(config['subscription'])
    subscription.update(host=server['subscription_domain'], yaml=generated['yaml'], raw=generated['raw'])
    bundle = build_bundle([subscription], revision)
    save_bundle(directory/'bundle.json', bundle)
    atomic_write(directory/'config.json', canonical({**config, 'server':server}))
    atomic_write(directory/'client.yaml', generated['yaml'].encode())
    run([str(BIN/'sing-box'), 'check', '-c', str(singbox)])
    run([str(BIN/'nginx'), '-e', 'stderr', '-p', str(ROOT/'nginx')+'/', '-t', '-c', str(directory/'nginx.conf')])
    run([str(BIN/'mihomo'), '-t', '-d', str(ROOT/'client-check'), '-f', str(directory/'client.yaml')])
    files = {str(path.relative_to(directory)): digest(path.read_bytes()) for path in directory.rglob('*') if path.is_file()}
    payload = {'revision':revision, 'files':files, 'site_domain':server['site_domain'],
               'subscription_domain':server['subscription_domain'], 'certificate_sha256':certificate['sha256'],
               'certificate_files': _certificate_files(server)}
    plan_id = store.plan('deployment', payload, expected_revision=base_revision)
    return {'plan_id':plan_id,'revision':revision,'site_domain':server['site_domain'],
            'subscription_domain':server['subscription_domain'],'checks':['certificate','sing-box','nginx','mihomo'],
            'changes':['managed website','REALITY listener','subscription bundle','ProxyHub service activation']}


def _link(directory: Path) -> None:
    temp = ROOT/('.current-'+secrets.token_hex(8))
    temp.symlink_to(directory)
    os.replace(temp, ROOT/'current')
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(directory: Path) -> None:
    import pwd
    target = ROOT/'publish/bundle.json'
    save_bundle(target, json.loads((directory/'bundle.json').read_text(encoding='utf-8')))
    os.chown(target, 0, pwd.getpwnam('proxyhub-pub').pw_gid)
    target.chmod(0o640)


def _units() -> tuple[str, ...]:
    from .installer import UNITS
    return UNITS


def _managed_revision(path: str | Path) -> Path:
    directory = Path(path)
    revisions = ROOT / 'revisions'
    if (not directory.is_absolute() or directory.parent != revisions
            or not re.fullmatch(r'[0-9a-f]{32}', directory.name)
            or ROOT.is_symlink() or revisions.is_symlink()
            or directory.is_symlink() or not directory.is_dir()):
        raise OperationError('Revision is not a managed immutable directory')
    return directory


def _current_revision() -> Path | None:
    current = ROOT / 'current'
    if current.is_symlink():
        # readlink avoids silently accepting a link that traverses outside the
        # managed tree and then re-enters it through another symlink.
        return _managed_revision(current.readlink())
    if current.exists():
        raise OperationError('Current revision path is not a managed link')
    return None


def _sync_root() -> None:
    if os.name == 'posix':
        fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _clear_current() -> None:
    current = ROOT / 'current'
    if current.is_symlink():
        current.unlink()
        _sync_root()
    elif current.exists():
        raise OperationError('Current revision path is not a managed link')


def _certificate_files(server: dict) -> dict:
    result = {}
    for key in ('certificate_path', 'key_path'):
        path = Path(server[key])
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise OperationError('Certificate must be a regular managed file')
        result[key] = digest(path.read_bytes())
    return result


def _check_ports(server: dict) -> None:
    for port in (server['http_port'], server['public_port'], server['reality_port'],
                 server['site_tls_port'], server['subscription_tls_port'], server['publisher_port']):
        for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
            try:
                with socket.socket(family) as test:
                    if family == socket.AF_INET6:
                        test.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    test.bind((address, port))
            except OSError as exc:
                raise OperationError(f'TCP port {port} is unavailable; no service was replaced') from exc


def _restore(previous: Path | None, candidate: Path) -> None:
    current = _current_revision()
    if current not in (None, previous, candidate):
        raise OperationError('Refusing to overwrite an unexpected current revision')
    if previous is not None:
        config = json.loads((previous / 'config.json').read_text(encoding='utf-8'))
        _link(previous)
        _publish(previous)
        for service in _units():
            run(['systemctl', 'restart', service])
        # systemctl restart acknowledges process startup, not HTTPS readiness.
        # Keep the activation pending until every restored consumer is healthy.
        _wait_health(config['server'])
    else:
        # Every service is attempted even if one stop fails. The journal is left
        # pending when any action fails, so a later recover can safely retry.
        errors = []
        for service in reversed(_units()):
            try:
                run(['systemctl', 'stop', service])
            except Exception as exc:
                errors.append(exc)
        try:
            run(['systemctl', 'disable', *_units()])
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise OperationError('Service rollback incomplete; run deployment recover') from errors[0]
        _clear_current()
        bundle = ROOT / 'publish/bundle.json'
        if bundle.is_symlink():
            raise OperationError('Unsafe publication path during rollback')
        bundle.unlink(missing_ok=True)
        if os.name == 'posix':
            fd = os.open(bundle.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def recover(store: StateStore) -> dict:
    """Conservatively undo an interrupted activation; never continue it."""
    with store.exclusive():
        pending = store.pending()
        if not pending:
            return {'status': 'nothing_to_recover'}
        if len(pending) != 1:
            raise OperationError('Multiple pending activations require manual investigation')
        item = pending[0]
        record = store.inspect(item['plan_id'])
        if record['kind'] != 'deployment' or record['status'] != 'prepared':
            raise OperationError('Pending activation has an inconsistent plan state')
        details = item['details']
        candidate = _managed_revision(details['candidate'])
        if candidate.name != record['payload']['revision']:
            raise OperationError('Recovery candidate differs from its plan')
        previous = _managed_revision(details['previous']) if details['previous'] else None
        _restore(previous, candidate)
        store.abort(item['plan_id'], {'recovered': True, 'previous_available': previous is not None})
        return {'status': 'recovered', 'previous_available': previous is not None}


def _health(server: dict) -> None:
    context = ssl.create_default_context()
    for domain, port in ((server['site_domain'],server['site_tls_port']),
                         (server['subscription_domain'],server['subscription_tls_port'])):
        with socket.create_connection(('127.0.0.1',port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=domain) as stream:
                stream.sendall(f'GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n'.encode())
                status = stream.recv(2048)
                expected = b'200' if domain == server['site_domain'] else b'404'
                if expected not in status.split(b'\r\n',1)[0]:
                    raise OperationError('Website or subscription HTTPS health check failed')
    with socket.create_connection(('127.0.0.1',server['reality_port']),timeout=5):
        pass


def _wait_health(server: dict) -> None:
    """Bound startup retries for both activation and restoration."""
    for attempt in range(15):
        try:
            _health(server)
            return
        except (OSError, OperationError):
            if attempt == 14:
                raise
            time.sleep(0.2)


def apply(store: StateStore, identity: str) -> dict:
    with store.exclusive():
        kind, payload = store.get(identity)
        if kind != 'deployment':
            raise OperationError('Plan type is not deployment')
        directory = _managed_revision(ROOT/'revisions'/payload['revision'])
        if set(payload['files']) != set(CANDIDATE_FILES):
            raise OperationError('Candidate file manifest is invalid')
        for name, expected in payload['files'].items():
            file = directory/name
            if file.parent.is_symlink() or file.is_symlink() or not file.is_file() or digest(file.read_bytes()) != expected:
                raise OperationError('Candidate file changed after validation')
        config = json.loads((directory/'config.json').read_text(encoding='utf-8'))
        server = config['server']
        certificate = validate_certificate(server['certificate_path'], server['key_path'],
                                           [server['site_domain'], server['subscription_domain']])
        if (certificate['sha256'] != payload['certificate_sha256']
                or _certificate_files(server) != payload['certificate_files']):
            raise OperationError('Deployment certificate changed after validation')
        previous = _current_revision()
        if previous is None:
            _check_ports(server)
        store.record(identity,'activating',{'previous':str(previous) if previous else None,'candidate':str(directory)})
        try:
            _link(directory)
            _publish(directory)
            for service in _units():
                run(['systemctl','restart',service])
            _wait_health(server)
            run(['systemctl','enable',*_units()])
            store.commit(identity)
        except Exception:
            # A successful database COMMIT is the boundary. Never undo it if a
            # later Python exception occurs; recovery sees no pending activation.
            if store.inspect(identity)['status'] != 'applied':
                _restore(previous, directory)
                store.abort(identity, {'previous_available': previous is not None})
            raise
        return {'status':'applied','revision':payload['revision'],'runtime_health':'passed','real_client_acceptance':'required'}
