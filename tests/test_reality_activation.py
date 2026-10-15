"""Independent REALITY entry activation preserves existing entry processes."""
from copy import deepcopy
import json
import os
import socket
import stat

import pytest

from proxyhub import deployment, reality
from proxyhub.core import OperationError, digest
from test_multisite_activation import multisite, two_sites, activate, SITE
from test_subscription_activation import environment, read_config


@pytest.fixture
def reality_environment(multisite, monkeypatch):
    env = multisite
    env.observed_reality = []
    env.target_probes = []
    env.real_check_reality_ports = deployment._check_reality_ports
    monkeypatch.setattr(deployment, '_check_reality_ports', lambda config, previous: None)
    monkeypatch.setattr(reality, 'probe_targets', lambda config: env.target_probes.append(deepcopy(config)))
    monkeypatch.setattr(deployment, '_wait_reality_health', lambda config, directory:
                        env.observed_reality.append((deepcopy(config), directory)), raising=False)
    config = two_sites(env)
    config['web']['sites'][1]['domains'].append('alias.example.test')
    config['web']['certificates'][1]['domains'].append('alias.example.test')
    activate(env, config)
    env.previous = env.pointer['current']
    env.calls.clear()
    return env


def migration(env):
    config = reality.change(read_config(env.pointer['current']), 'reality.migrate',
                            {'type': 'site', 'site_id': SITE, 'sni': 'second.example.test'})
    return config, config['reality']['primary_endpoint_id']


def prepare(env, config, kind, endpoint_id):
    result = deployment.prepare(env.store, config,
        reality_operation={'kind': kind, 'endpoint_id': endpoint_id})
    return result, env.root / 'revisions' / result['revision']


def migrated(env):
    config, endpoint_id = migration(env)
    result, directory = prepare(env, config, 'migrate', endpoint_id)
    deployment.apply(env.store, result['plan_id'])
    env.calls.clear()
    return config, endpoint_id, directory


def service_call(env, action, service):
    return any(call[:2] == ('systemctl', action) and service in call[2:] for call in env.calls)


def assert_no_existing_restart(env):
    assert not any('restart' in call for call in env.calls)
    assert not service_call(env, 'stop', 'proxyhub-proxy.service')
    assert not service_call(env, 'disable', 'proxyhub-proxy.service')


def test_migration_renders_independent_config_and_preserves_existing_services(reality_environment):
    env = reality_environment
    original = read_config(env.previous)
    config, endpoint_id = migration(env)
    prepared, directory = prepare(env, config, 'migrate', endpoint_id)
    relative = f'reality/{endpoint_id}.json'
    payload = env.store.inspect(prepared['plan_id'])['payload']
    assert relative in payload['files']
    previous_node = json.loads((directory / 'sing-box.json').read_text())
    next_node = json.loads((directory / relative).read_text())
    assert previous_node['inbounds'][0]['tls']['server_name'] == original['server']['site_domain']
    assert next_node['inbounds'][0]['tls']['server_name'] == 'second.example.test'
    assert next_node['inbounds'][0]['users'] == previous_node['inbounds'][0]['users']
    assert next_node['inbounds'][0]['tls']['reality']['private_key'] == previous_node['inbounds'][0]['tls']['reality']['private_key']
    assert next_node['inbounds'][0]['listen_port'] != previous_node['inbounds'][0]['listen_port']
    env.calls.clear()
    result = deployment.apply(env.store, prepared['plan_id'])
    assert result['activation'] == 'reality'
    assert service_call(env, 'start', f'proxyhub-reality@{endpoint_id}.service')
    assert service_call(env, 'reload', 'proxyhub-nginx.service')
    assert service_call(env, 'reload', 'proxyhub-publisher.service')
    assert_no_existing_restart(env)
    current = read_config(directory)
    assert all(current[key] == original[key] for key in ('server', 'web', 'subscription', 'overrides'))
    assert [entry['id'] for entry in env.observed_reality[-1][0]['reality']['endpoints']] == [endpoint_id]
    assert len(current['reality']['endpoints']) == 2
    assert env.observed_reality[-1][1] == directory
    assert env.target_probes and env.store.pending() == []


@pytest.mark.parametrize('mutation', ['changed', 'missing', 'extra-file', 'missing-manifest', 'extra-manifest'])
def test_extra_entry_config_and_manifest_tampering_fail_before_service_changes(reality_environment, mutation):
    env = reality_environment
    config, endpoint_id = migration(env)
    prepared, directory = prepare(env, config, 'migrate', endpoint_id)
    relative = f'reality/{endpoint_id}.json'
    identity = prepared['plan_id']
    if mutation == 'changed':
        (directory / relative).write_text('{}')
    elif mutation == 'missing':
        (directory / relative).unlink()
    elif mutation == 'extra-file':
        (directory / 'reality' / ('f' * 32 + '.json')).write_text('{}')
    else:
        payload = env.store.inspect(identity)['payload']
        if mutation == 'missing-manifest':
            payload['files'].pop(relative)
        else:
            payload['files']['reality/' + 'f' * 32 + '.json'] = '0' * 64
        identity = env.store.plan('deployment', payload)
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.apply(env.store, identity)
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


@pytest.mark.parametrize('mode', ['full', 'web', 'publication'])
@pytest.mark.parametrize('mutation', ['add', 'remove', 'target', 'listener'])
def test_raw_deployment_cannot_change_reality_registry(reality_environment, mode, mutation):
    env = reality_environment
    if mutation == 'add':
        config, _ = migration(env)
    else:
        _, endpoint_id, _ = migrated(env)
        config = read_config(env.pointer['current'])
        if mutation == 'remove':
            config = reality.change(config, 'reality.select', {'endpoint_id': 'default'})
            config = reality.change(config, 'reality.retire', {'endpoint_id': endpoint_id})
        elif mutation == 'target':
            endpoint = reality.get_endpoint(config['reality'], endpoint_id)
            reality.get_target(config['reality'], endpoint['target_id'])['server_name'] = 'alias.example.test'
        else:
            reality.get_endpoint(config['reality'], endpoint_id)['listen_port'] += 1
    current = env.pointer['current']
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.prepare(env.store, config, web_only=mode == 'web', publish_only=mode == 'publication')
    assert env.calls == [] and env.pointer['current'] == current and env.store.pending() == []


@pytest.mark.parametrize('section,field,value', [
    ('server', 'node_name', 'Changed node'), ('subscription', 'name', 'Changed subscription'),
    ('overrides', 'mixed-port', 17891), ('web', 'title', 'Changed website'),
])
def test_reality_mode_cannot_carry_unrelated_changes(reality_environment, section, field, value):
    env = reality_environment
    config, endpoint_id = migration(env)
    if section == 'web':
        config['web']['sites'][1][field] = value
    else:
        config[section][field] = value
    env.calls.clear()
    with pytest.raises(OperationError):
        prepare(env, config, 'migrate', endpoint_id)
    assert env.calls == [] and env.pointer['current'] == env.previous


def test_existing_target_cannot_change_under_a_migration_operation(reality_environment):
    env = reality_environment
    _, endpoint_id, _ = migrated(env)
    config = read_config(env.pointer['current'])
    endpoint = reality.get_endpoint(config['reality'], endpoint_id)
    reality.get_target(config['reality'], endpoint['target_id'])['server_name'] = 'alias.example.test'
    env.calls.clear()
    with pytest.raises(OperationError):
        prepare(env, config, 'migrate', endpoint_id)
    assert env.calls == []


def test_selection_republishes_retained_entry_without_replacing_any_entry(reality_environment):
    env = reality_environment
    config, endpoint_id, _ = migrated(env)
    selected = reality.change(config, 'reality.select', {'endpoint_id': 'default'})
    prepared, directory = prepare(env, selected, 'select', 'default')
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert service_call(env, 'reload', 'proxyhub-publisher.service')
    assert not any(call[:2] == ('systemctl', action) for call in env.calls for action in ('start', 'stop', 'disable', 'restart'))
    assert read_config(directory)['reality']['primary_endpoint_id'] == 'default'
    assert {item['id'] for item in read_config(directory)['reality']['endpoints']} == {'default', endpoint_id}


def test_retirement_waits_for_surviving_entry_health_before_stopping_old_service(reality_environment, monkeypatch):
    env = reality_environment
    config, endpoint_id, _ = migrated(env)
    selected = reality.change(config, 'reality.select', {'endpoint_id': 'default'})
    prepared, _ = prepare(env, selected, 'select', 'default')
    deployment.apply(env.store, prepared['plan_id'])
    retired = reality.change(read_config(env.pointer['current']), 'reality.retire', {'endpoint_id': endpoint_id})
    prepared, directory = prepare(env, retired, 'retire', endpoint_id)
    unit = f'proxyhub-reality@{endpoint_id}.service'
    def health(config, candidate):
        assert candidate == directory
        assert not service_call(env, 'stop', unit)
        env.observed_reality.append((deepcopy(config), candidate))
    monkeypatch.setattr(deployment, '_wait_reality_health', health)
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert service_call(env, 'stop', unit) and service_call(env, 'disable', unit)
    assert_no_existing_restart(env)
    assert [item['id'] for item in read_config(directory)['reality']['endpoints']] == ['default']


@pytest.mark.parametrize('only_entry', [True, False])
def test_current_primary_and_last_entry_cannot_be_retired(reality_environment, only_entry):
    env = reality_environment
    if only_entry:
        config, endpoint_id = read_config(env.previous), 'default'
    else:
        config, endpoint_id, _ = migrated(env)
    env.calls.clear()
    with pytest.raises((OperationError, ValueError)):
        prepare(env, config, 'retire', endpoint_id)
    assert env.calls == [] and env.store.pending() == []


def test_new_entry_port_taken_after_preparation_fails_without_replacing_services(reality_environment, monkeypatch):
    env = reality_environment
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(('0.0.0.0', 0))
        occupied.listen()
        config, endpoint_id = migration(env)
        reality.get_endpoint(config['reality'], endpoint_id)['listen_port'] = occupied.getsockname()[1]
        prepared, _ = prepare(env, config, 'migrate', endpoint_id)
        monkeypatch.setattr(deployment, '_check_reality_ports', env.real_check_reality_ports)
        env.calls.clear()
        with pytest.raises(OperationError, match='unavailable'):
            deployment.apply(env.store, prepared['plan_id'])
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


def test_failed_new_entry_health_restores_previous_before_stopping_candidate(reality_environment, monkeypatch):
    env = reality_environment
    config, endpoint_id = migration(env)
    prepared, candidate = prepare(env, config, 'migrate', endpoint_id)
    unit = f'proxyhub-reality@{endpoint_id}.service'
    observed = []
    def health(config, directory):
        observed.append(directory)
        if directory == candidate:
            raise OperationError('New SNI health failed')
        assert directory == env.previous and env.pointer['current'] == env.previous
        assert not service_call(env, 'stop', unit)
    monkeypatch.setattr(deployment, '_wait_reality_health', health)
    env.calls.clear()
    with pytest.raises(OperationError, match='New SNI health failed'):
        deployment.apply(env.store, prepared['plan_id'])
    assert observed == [candidate, env.previous]
    assert service_call(env, 'stop', unit) and service_call(env, 'disable', unit)
    assert_no_existing_restart(env)
    assert env.pointer['current'] == env.previous and env.store.pending() == []
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed'


@pytest.mark.parametrize('failure', [None, 'certificate', 'content'])
def test_public_sni_health_checks_both_legacy_and_new_website(reality_environment, monkeypatch, failure):
    env = reality_environment
    config, endpoint_id = migration(env)
    _, directory = prepare(env, config, 'migrate', endpoint_id)
    names, destinations, requests = [], [], []
    bodies = {config['server']['site_domain']: (directory / 'site/index.html').read_bytes(),
              'second.example.test': (directory / 'sites' / SITE / 'index.html').read_bytes()}
    class Stream:
        def __init__(self, name=''):
            self.name = name
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def getpeercert(self, *, binary_form):
            assert binary_form
            return b'wrong certificate' if failure == 'certificate' and self.name == 'second.example.test' else self.name.encode()
        def sendall(self, data):
            requests.append((self.name, data))
    class Context:
        def wrap_socket(self, raw, *, server_hostname):
            names.append(server_hostname)
            return Stream(server_hostname)
    class Response:
        status = 200
        def __init__(self, stream):
            self.name = stream.name
        def begin(self):
            pass
        def read(self, limit):
            return b'wrong body' if failure == 'content' and self.name == 'second.example.test' else bodies[self.name]
        def close(self):
            pass
    context = Context()
    def connect(destination, *, timeout):
        destinations.append(destination)
        return Stream()
    monkeypatch.setattr(deployment.socket, 'create_connection', connect)
    monkeypatch.setattr(deployment.ssl, 'create_default_context', lambda: context)
    monkeypatch.setattr(deployment.http.client, 'HTTPResponse', Response)
    monkeypatch.setattr(deployment, 'validate_certificate', lambda certificate, key, domains: {'sha256': digest(domains[0].encode())})
    if failure:
        with pytest.raises(OperationError, match='REALITY SNI route'):
            deployment._reality_health(config, directory)
    else:
        deployment._reality_health(config, directory)
    assert names == [config['server']['site_domain'], 'second.example.test']
    assert destinations == [('127.0.0.1', config['server']['public_port'])] * 2
    assert context.minimum_version == context.maximum_version == deployment.ssl.TLSVersion.TLSv1_3
    assert requests[0][1].startswith(b'GET / HTTP/1.1\r\nHost: site.example.test\r\n')


def test_failed_restore_preserves_pending_until_recovery_can_confirm_previous_health(reality_environment, monkeypatch):
    env = reality_environment
    config, endpoint_id = migration(env)
    prepared, candidate = prepare(env, config, 'migrate', endpoint_id)
    unit = f'proxyhub-reality@{endpoint_id}.service'
    def unavailable(*args):
        raise OperationError('REALITY health unavailable')
    monkeypatch.setattr(deployment, '_wait_reality_health', unavailable)
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.apply(env.store, prepared['plan_id'])
    assert env.store.pending() and env.store.inspect(prepared['plan_id'])['status'] == 'prepared'
    assert not service_call(env, 'stop', unit)
    with pytest.raises(OperationError):
        deployment.recover(env.store)
    assert env.store.pending()
    monkeypatch.setattr(deployment, '_wait_reality_health', lambda config, directory: None)
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == env.previous and env.store.pending() == []
    assert service_call(env, 'stop', unit) and service_call(env, 'disable', unit)
    assert_no_existing_restart(env)


def test_interrupted_retirement_restarts_retained_entry_before_routing_is_restored(reality_environment):
    env = reality_environment
    config, endpoint_id, previous = migrated(env)
    retired = reality.change(config, 'reality.retire', {'endpoint_id': 'default'})
    prepared, candidate = prepare(env, retired, 'retire', 'default')
    env.store.record(prepared['plan_id'], 'activating', {
        'previous': str(previous), 'candidate': str(candidate), 'activation': 'reality',
    })
    env.pointer['current'] = candidate
    env.calls.clear()
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert service_call(env, 'start', 'proxyhub-proxy.service')
    assert service_call(env, 'start', f'proxyhub-reality@{endpoint_id}.service')
    assert_no_existing_restart(env)
    assert env.pointer['current'] == previous and env.store.pending() == []


@pytest.mark.skipif(os.name != 'posix', reason='Real Unix directory modes')
def test_extra_entry_configuration_has_private_runtime_permissions(reality_environment):
    env = reality_environment
    config, endpoint_id = migration(env)
    prepared, directory = prepare(env, config, 'migrate', endpoint_id)
    deployment._check_runtime_permissions(directory)
    assert (directory / 'reality').stat().st_mode & stat.S_IXGRP
    assert stat.S_IMODE((directory / 'reality' / f'{endpoint_id}.json').stat().st_mode) == 0o640


def test_failed_external_entry_can_select_healthy_legacy_then_retire(reality_environment, monkeypatch):
    env = reality_environment
    monkeypatch.setattr(reality, 'resolve_external', lambda *args: '1.1.1.1')
    config = reality.change(read_config(env.pointer['current']), 'reality.migrate', {
        'type': 'external', 'address': 'target.example.test', 'port': 443,
        'sni': 'external.example.test'})
    external_id = config['reality']['primary_endpoint_id']
    initial, _ = prepare(env, config, 'migrate', external_id)
    deployment.apply(env.store, initial['plan_id'])
    env.calls.clear()

    def healthy_local_only(config):
        if any(target['type'] == 'external' for target in reality.normalize_reality(config)['targets']):
            raise OperationError('Retained external target is now unavailable')
    monkeypatch.setattr(reality, 'probe_targets', healthy_local_only)
    monkeypatch.setattr(deployment, '_wait_reality_health', lambda config, directory: healthy_local_only(config))
    selected = reality.change(read_config(env.pointer['current']), 'reality.select', {'endpoint_id': 'default'})
    selection, selected_directory = prepare(env, selected, 'select', 'default')
    deployment.apply(env.store, selection['plan_id'])
    assert read_config(selected_directory)['reality']['primary_endpoint_id'] == 'default'
    assert len(read_config(selected_directory)['reality']['endpoints']) == 2
    unit = f'proxyhub-reality@{external_id}.service'
    assert not service_call(env, 'stop', unit)
    assert_no_existing_restart(env)

    retired = reality.change(read_config(selected_directory), 'reality.retire', {'endpoint_id': external_id})
    retirement, directory = prepare(env, retired, 'retire', external_id)
    deployment.apply(env.store, retirement['plan_id'])
    assert [entry['id'] for entry in read_config(directory)['reality']['endpoints']] == ['default']
    assert service_call(env, 'stop', unit) and service_call(env, 'disable', unit)
    assert env.store.pending() == []


def test_publication_ignores_existing_external_target_outage(reality_environment, monkeypatch):
    env = reality_environment
    monkeypatch.setattr(reality, 'resolve_external', lambda *args: '1.1.1.1')
    config = reality.change(read_config(env.pointer['current']), 'reality.migrate', {
        'type': 'external', 'address': 'target.example.test', 'port': 443,
        'sni': 'external.example.test'})
    initial, _ = prepare(env, config, 'migrate', config['reality']['primary_endpoint_id'])
    deployment.apply(env.store, initial['plan_id'])
    env.calls.clear()

    def unrelated_target_check(*args):
        raise AssertionError('Publication must not depend on target network health')
    monkeypatch.setattr(reality, 'probe_targets', unrelated_target_check)
    monkeypatch.setattr(deployment, '_wait_reality_health', unrelated_target_check)
    updated = read_config(env.pointer['current'])
    updated['subscription']['name'] = 'Metadata during target outage'
    prepared = deployment.prepare(env.store, updated, publish_only=True)
    deployment.apply(env.store, prepared['plan_id'])
    assert env.calls[-2:] == [
        ('publish', env.root / 'revisions' / prepared['revision']),
        ('systemctl', 'reload', 'proxyhub-publisher.service')]
    assert not service_call(env, 'reload', 'proxyhub-nginx.service')
    assert_no_existing_restart(env)


def test_web_certificate_replacement_does_not_probe_expired_running_target(reality_environment, monkeypatch):
    env = reality_environment
    migrated(env)
    updated = read_config(env.pointer['current'])
    binding = updated['web']['certificates'][1]
    binding['certificate_path'] = '/var/lib/proxyhub/certificates/' + 'e' * 64 + '/fullchain.pem'
    binding['key_path'] = '/var/lib/proxyhub/certificates/' + 'e' * 64 + '/privkey.pem'

    def expired_running_target(*args):
        raise OperationError('The running target still serves its expired certificate')
    monkeypatch.setattr(reality, 'probe_targets', expired_running_target)
    prepared = deployment.prepare(env.store, updated, web_only=True)
    deployment.apply(env.store, prepared['plan_id'])
    active = read_config(env.pointer['current'])
    assert active['web']['certificates'][1]['certificate_path'] == binding['certificate_path']
    assert env.observed_health[-1][0]['web']['certificates'][1] == binding
    assert_no_existing_restart(env)
