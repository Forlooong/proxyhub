"""HY2 lifecycle transactions keep existing TCP services and identities intact."""
from copy import deepcopy
import json
import socket

import pytest

from proxyhub import deployment, hy2
from proxyhub.core import OperationError
from proxyhub.websites import get_certificate
from test_multisite_activation import multisite, two_sites, activate, CERTIFICATE
from test_subscription_activation import environment, read_config


@pytest.fixture
def hy2_environment(multisite, monkeypatch):
    env = multisite
    env.hy2_health = []
    env.real_check_hy2_ports = deployment._check_hy2_ports
    monkeypatch.setattr(deployment, '_certificate_material', lambda path:
                        ['KEY-FIXTURE', path] if path.endswith('privkey.pem') else ['CERTIFICATE-FIXTURE', path])
    monkeypatch.setattr(deployment, '_check_hy2_ports', lambda config, previous: None)
    monkeypatch.setattr(deployment, '_wait_hy2_health', lambda config, directory:
                        env.hy2_health.append((deepcopy(config), directory)))
    activate(env, two_sites(env))
    env.previous = env.pointer['current']
    env.calls.clear()
    return env


def prepare(env, config, kind):
    result = deployment.prepare(env.store, config, hy2_operation={'kind': kind})
    return result, env.root / 'revisions' / result['revision']


def create(env):
    config = hy2.change(read_config(env.pointer['current']), 'hy2.create', {
        'server_name': 'second.example.test', 'certificate_id': CERTIFICATE,
    })
    prepared, directory = prepare(env, config, 'create')
    deployment.apply(env.store, prepared['plan_id'])
    env.calls.clear()
    env.hy2_health.clear()
    return read_config(directory)


def assert_existing_tcp_services_untouched(env):
    for call in env.calls:
        if call[0] == 'systemctl':
            assert not any(unit.startswith(('proxyhub-proxy.', 'proxyhub-reality@', 'proxyhub-nginx.'))
                           for unit in call[2:])


@pytest.mark.parametrize('kind,action', [('create', 'start'), ('update', 'restart'),
                                         ('disable', 'stop'), ('enable', 'start')])
def test_lifecycle_adjusts_only_hy2_and_publication(hy2_environment, kind, action):
    env = hy2_environment
    before = read_config(env.previous) if kind == 'create' else create(env)
    if kind == 'enable':
        disabled = hy2.change(before, 'hy2.disable', {})
        prepared, _ = prepare(env, disabled, 'disable')
        deployment.apply(env.store, prepared['plan_id'])
        before = read_config(env.pointer['current'])
    values = ({'server_name': 'second.example.test', 'certificate_id': CERTIFICATE} if kind == 'create'
              else {'name': 'Updated HY2'} if kind == 'update' else {})
    config = hy2.change(before, 'hy2.' + kind, values)
    prepared, directory = prepare(env, config, kind)
    payload = env.store.inspect(prepared['plan_id'])['payload']
    assert payload['activation'] == 'hy2' and payload['hy2_operation'] == {'kind': kind}
    assert 'hy2.json' in payload['files']
    rendered = json.loads((directory / 'hy2.json').read_text())
    assert rendered['inbounds'][0]['users'][0]['password'] == config['hy2']['password']
    binding = get_certificate(config['web'], config['hy2']['certificate_id'])
    tls = rendered['inbounds'][0]['tls']
    assert tls['certificate'] == ['CERTIFICATE-FIXTURE', binding['certificate_path']]
    assert tls['key'] == ['KEY-FIXTURE', binding['key_path']]
    assert 'certificate_path' not in tls and 'key_path' not in tls
    env.calls.clear()
    env.hy2_health.clear()
    result = deployment.apply(env.store, prepared['plan_id'])
    assert result['activation'] == 'hy2'
    assert ('systemctl', action, 'proxyhub-hy2.service') in env.calls
    assert ('systemctl', 'reload', 'proxyhub-publisher.service') in env.calls
    assert_existing_tcp_services_untouched(env)
    current = read_config(directory)
    assert all(current[key] == before[key] for key in ('server', 'web', 'subscription', 'overrides'))
    if kind != 'create':
        assert current['hy2']['password'] == before['hy2']['password']
    if current['hy2']['enabled']:
        assert env.hy2_health[-1] == (current, directory)
    assert env.store.pending() == []


@pytest.mark.parametrize('mode', ['full', 'web', 'publication'])
@pytest.mark.parametrize('mutation', ['create', 'password'])
def test_raw_modes_cannot_bypass_hy2_lifecycle(hy2_environment, mode, mutation):
    env = hy2_environment
    if mutation == 'create':
        config = hy2.change(read_config(env.previous), 'hy2.create', {
            'server_name': 'second.example.test', 'certificate_id': CERTIFICATE})
    else:
        config = create(env)
        config['hy2']['password'] = 'x' * 43
    previous = env.pointer['current']
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.prepare(env.store, config, web_only=mode == 'web', publish_only=mode == 'publication')
    assert env.calls == [] and env.pointer['current'] == previous and env.store.pending() == []


@pytest.mark.parametrize('mutation', ['password', 'subscription', 'website'])
def test_typed_hy2_update_rejects_unrelated_or_credential_changes(hy2_environment, mutation):
    env = hy2_environment
    config = create(env)
    config = hy2.change(config, 'hy2.update', {'name': 'Updated HY2'})
    if mutation == 'password':
        config['hy2']['password'] = 'x' * 43
    elif mutation == 'subscription':
        config['subscription']['name'] = 'Unrelated subscription change'
    else:
        config['web']['sites'][1]['title'] = 'Unrelated website change'
    env.calls.clear()
    with pytest.raises(OperationError):
        prepare(env, config, 'update')
    assert env.calls == [] and env.store.pending() == []


@pytest.mark.parametrize('mutation', ['changed', 'missing', 'manifest'])
def test_hy2_runtime_input_is_immutable_before_service_changes(hy2_environment, mutation):
    env = hy2_environment
    config = hy2.change(read_config(env.previous), 'hy2.create', {
        'server_name': 'second.example.test', 'certificate_id': CERTIFICATE})
    prepared, directory = prepare(env, config, 'create')
    identity = prepared['plan_id']
    if mutation == 'changed':
        (directory / 'hy2.json').write_text('{}')
    elif mutation == 'missing':
        (directory / 'hy2.json').unlink()
    else:
        payload = env.store.inspect(identity)['payload']
        payload['files'].pop('hy2.json')
        identity = env.store.plan('deployment', payload)
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.apply(env.store, identity)
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


@pytest.mark.parametrize('stage', ['prepare', 'apply'])
def test_occupied_udp_port_is_rejected_before_switch(hy2_environment, monkeypatch, stage):
    env = hy2_environment
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(('0.0.0.0', 0))
        config = hy2.change(read_config(env.previous), 'hy2.create', {
            'port': occupied.getsockname()[1], 'server_name': 'second.example.test',
            'certificate_id': CERTIFICATE})
        if stage == 'apply':
            prepared, _ = prepare(env, config, 'create')
        monkeypatch.setattr(deployment, '_check_hy2_ports', env.real_check_hy2_ports)
        env.calls.clear()
        with pytest.raises(OperationError, match='UDP port is unavailable'):
            if stage == 'prepare':
                prepare(env, config, 'create')
            else:
                deployment.apply(env.store, prepared['plan_id'])
    # Preparation can run validators but must not alter active services.
    assert not any(call[0] in ('systemctl', 'publish') for call in env.calls)
    assert env.pointer['current'] == env.previous and env.store.pending() == []


def test_certificate_replacement_refreshes_hy2_and_checks_selected_binding(hy2_environment):
    env = hy2_environment
    before = create(env)
    config = deepcopy(before)
    binding = get_certificate(config['web'], CERTIFICATE)
    binding.update(certificate_path='/var/lib/proxyhub/certificates/87654321/fullchain.pem',
                   key_path='/var/lib/proxyhub/certificates/87654321/privkey.pem')
    prepared = deployment.prepare(env.store, config, web_only=True)
    directory = env.root / 'revisions' / prepared['revision']
    runtime_tls = json.loads((directory / 'hy2.json').read_text())['inbounds'][0]['tls']
    assert runtime_tls['certificate'] == ['CERTIFICATE-FIXTURE', binding['certificate_path']]
    assert runtime_tls['key'] == ['KEY-FIXTURE', binding['key_path']]
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert ('systemctl', 'restart', 'proxyhub-hy2.service') in env.calls
    assert ('systemctl', 'reload', 'proxyhub-nginx.service') in env.calls
    assert not any('restart' in call and 'proxyhub-hy2.service' not in call for call in env.calls)
    assert env.hy2_health[-1] == (read_config(directory), directory)
    assert get_certificate(env.hy2_health[-1][0]['web'], CERTIFICATE) == binding
    assert read_config(directory)['hy2'] == before['hy2']


@pytest.mark.parametrize('mode', ['web', 'publication'])
def test_unrelated_changes_do_not_restart_or_probe_hy2(hy2_environment, mode):
    env = hy2_environment
    config = create(env)
    if mode == 'web':
        config['web']['sites'][1]['title'] = 'New website title'
    else:
        config['subscription']['name'] = 'New subscription title'
    prepared = deployment.prepare(env.store, config, web_only=mode == 'web', publish_only=mode == 'publication')
    env.calls.clear()
    env.hy2_health.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert not any('proxyhub-hy2.service' in call for call in env.calls)
    assert env.hy2_health == []


def test_failed_hy2_health_restores_previous_config_and_credentials(hy2_environment, monkeypatch):
    env = hy2_environment
    before = create(env)
    previous = env.pointer['current']
    config = hy2.change(before, 'hy2.update', {'name': 'Failed HY2 candidate'})
    prepared, candidate = prepare(env, config, 'update')
    observed = []
    def health(snapshot, directory):
        observed.append(directory)
        assert env.pointer['current'] == directory
        if directory == candidate:
            raise OperationError('HY2 candidate did not authenticate')
        assert snapshot == before
    monkeypatch.setattr(deployment, '_wait_hy2_health', health)
    env.calls.clear()
    with pytest.raises(OperationError, match='did not authenticate'):
        deployment.apply(env.store, prepared['plan_id'])
    assert observed == [candidate, previous]
    assert env.pointer['current'] == previous and read_config(previous) == before
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed' and env.store.pending() == []
    assert_existing_tcp_services_untouched(env)


@pytest.mark.parametrize('kind', ['create', 'disable'])
def test_interrupted_hy2_activation_recovers_and_repeated_recovery_is_noop(hy2_environment, kind):
    env = hy2_environment
    before = read_config(env.previous) if kind == 'create' else create(env)
    previous = env.pointer['current']
    values = {'server_name': 'second.example.test', 'certificate_id': CERTIFICATE} if kind == 'create' else {}
    config = hy2.change(before, 'hy2.' + kind, values)
    prepared, candidate = prepare(env, config, kind)
    env.store.record(prepared['plan_id'], 'activating', {
        'previous': str(previous), 'candidate': str(candidate), 'activation': 'hy2',
    })
    env.pointer['current'] = candidate
    env.calls.clear()
    revision = env.store.revision
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == previous and read_config(previous) == before
    assert env.store.revision == revision and env.store.pending() == []
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed'
    assert_existing_tcp_services_untouched(env)
    env.calls.clear()
    assert deployment.recover(env.store)['status'] == 'nothing_to_recover' and env.calls == []


def test_failed_restore_keeps_pending_until_previous_hy2_authenticates(hy2_environment, monkeypatch):
    env = hy2_environment
    before = create(env)
    previous = env.pointer['current']
    prepared, _ = prepare(env, hy2.change(before, 'hy2.update', {'name': 'Unavailable candidate'}), 'update')
    def unavailable(*args):
        raise OperationError('HY2 authentication unavailable')
    monkeypatch.setattr(deployment, '_wait_hy2_health', unavailable)
    with pytest.raises(OperationError):
        deployment.apply(env.store, prepared['plan_id'])
    assert env.store.pending()
    with pytest.raises(OperationError):
        deployment.recover(env.store)
    assert env.store.pending()
    monkeypatch.setattr(deployment, '_wait_hy2_health', lambda config, directory: None)
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == previous and env.store.pending() == []


def test_initial_full_deployment_cannot_start_hy2_without_typed_lifecycle(hy2_environment):
    env = hy2_environment
    config = hy2.change(read_config(env.previous), 'hy2.create', {
        'server_name': 'second.example.test', 'certificate_id': CERTIFICATE})
    env.pointer['current'] = None
    env.calls.clear()
    with pytest.raises(OperationError, match='HY2'):
        deployment.prepare(env.store, config)
    assert not any(call[0] in ('systemctl', 'publish') for call in env.calls)
    assert env.pointer['current'] is None and env.store.pending() == []


@pytest.mark.parametrize('failure', [None, 'trust', 'pin'])
def test_hy2_health_requires_both_pki_name_and_selected_leaf(hy2_environment, monkeypatch, failure):
    env = hy2_environment
    config = create(env)
    probes = []
    fingerprint = deployment._certificate_bindings(config)[CERTIFICATE]['sha256']
    def probe(snapshot, fingerprint=None):
        assert snapshot == config
        probes.append(fingerprint)
        if (failure == 'trust' and fingerprint is None) or (failure == 'pin' and fingerprint is not None):
            raise OperationError('HY2 certificate verification failed')
    monkeypatch.setattr(deployment, '_hy2_probe', probe)
    if failure:
        with pytest.raises(OperationError, match='verification failed'):
            deployment._hy2_health(config, env.pointer['current'])
    else:
        deployment._hy2_health(config, env.pointer['current'])
    assert probes == ([None] if failure == 'trust' else [None, fingerprint])


def test_hy2_probe_reaches_configured_publisher_when_proxy_uses_host_authority(hy2_environment, monkeypatch):
    import threading
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    env = hy2_environment
    config = hy2.change(read_config(env.previous), 'hy2.create', {
        'server_name': 'second.example.test', 'certificate_id': CERTIFICATE})
    config['server']['publisher_port'] = 19991
    client, proxy = socket.socketpair()
    observed = []
    def serve_proxy():
        with proxy:
            proxy.settimeout(3)
            request = b''
            while b'\r\n\r\n' not in request:
                chunk = proxy.recv(4096)
                if not chunk:
                    return
                request += chunk
            lines = request.decode('ascii').split('\r\n')
            requested = urlsplit(lines[0].split()[1])
            host = next(line.split(':', 1)[1].strip() for line in lines if line.lower().startswith('host:'))
            # HTTP proxies may use Host as the upstream authority. Only the
            # configured publisher produces the expected managed 404 response.
            upstream = urlsplit('http://' + host)
            observed.append(((requested.hostname, requested.port), (upstream.hostname, upstream.port or 80)))
            status = b'404 Not Found' if (upstream.hostname, upstream.port or 80) == ('127.0.0.1', 19991) else b'502 Bad Gateway'
            proxy.sendall(b'HTTP/1.1 ' + status + b'\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    worker = threading.Thread(target=serve_proxy, daemon=True)
    worker.start()
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda **kwargs: None)
    monkeypatch.setattr(deployment.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(deployment.socket, 'create_connection', lambda *args, **kwargs: client)
    try:
        deployment._hy2_probe(config)
    finally:
        client.close()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert observed == [(('127.0.0.1', 19991), ('127.0.0.1', 19991))]
