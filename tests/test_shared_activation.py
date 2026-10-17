"""Fixed shared-runtime migration preserves resources and has a recoverable boundary."""
from copy import deepcopy
import json

import pytest

from proxyhub import deployment, hy2, nodes
from proxyhub.core import OperationError
from test_node_activation import add as add_node, node_environment
from test_hy2_activation import hy2_environment
from test_multisite_activation import multisite, CERTIFICATE
from test_subscription_activation import environment, read_config


SHARED = 'proxyhub-shared.service'
REAL_WAIT_HY2_HEALTH = deployment._wait_hy2_health
REAL_WAIT_NODES_HEALTH = deployment._wait_nodes_health


class Interrupted(BaseException):
    """A process exit bypasses the ordinary exception rollback path."""


@pytest.fixture(autouse=True)
def isolated_migration_health(node_environment, monkeypatch):
    env = node_environment
    env.isolated_health = []
    monkeypatch.setattr(deployment, '_isolated_runtime_health', lambda config, directory:
                        env.isolated_health.append((deepcopy(config), directory, env.pointer['current'])))


def prepare_runtime(env):
    result = deployment.prepare(env.store, read_config(env.pointer['current']), runtime_only=True)
    return result, env.root / 'revisions' / result['revision']


def migrate(env):
    prepared, directory = prepare_runtime(env)
    deployment.apply(env.store, prepared['plan_id'])
    env.calls.clear()
    env.hy2_health.clear()
    env.node_health.clear()
    return directory


def service_calls(env):
    return [call for call in env.calls if call[0] == 'systemctl']


def assert_original_services_untouched(env):
    assert not any(name.startswith(('proxyhub-proxy.', 'proxyhub-reality@', 'proxyhub-nginx.'))
                   for call in service_calls(env) for name in call[2:])


def assert_group_health(env, config, directory):
    assert env.hy2_health[-1] == (config, directory)
    snapshot, checked, identities = env.node_health[-1]
    assert snapshot == config and checked == directory
    enabled = {node['id'] for node in config['nodes'] if node['enabled']}
    assert identities is None or identities == enabled


def assert_shared_restarted_once(env):
    starts = [call for call in service_calls(env) if SHARED in call and call[1] in {'start', 'restart'}]
    assert len(starts) == 1
    assert_original_services_untouched(env)


def test_explicit_migration_keeps_resources_and_checks_every_enabled_member(node_environment):
    env = node_environment
    previous = env.pointer['current']
    before = read_config(previous)
    assert not (previous / 'shared.json').exists()
    prepared, directory = prepare_runtime(env)
    payload = env.store.inspect(prepared['plan_id'])['payload']
    assert payload['activation'] == prepared['activation'] == 'runtime'
    assert 'shared.json' in payload['files']
    impact = prepared['shared_runtime']
    assert impact['changed'] is True and impact['reconnect_required'] is True
    expected = {before['hy2']['id'], *(node['id'] for node in before['nodes'] if node['enabled'])}
    assert {member['id'] for member in impact['members']} == expected
    assert all(set(member) == {'id', 'name', 'protocol'} for member in impact['members'])
    shared = json.loads((directory / 'shared.json').read_text())
    assert {inbound['tag'] for inbound in shared['inbounds']} == {
        before['hy2']['listener_id'], *(node['listener_id'] for node in before['nodes'] if node['enabled'])}
    assert read_config(directory) == before
    env.calls.clear()
    env.hy2_health.clear()
    env.node_health.clear()
    revision = env.store.revision
    assert deployment.apply(env.store, prepared['plan_id'])['activation'] == 'runtime'
    assert env.pointer['current'] == directory and env.store.revision == revision + 1
    assert env.store.pending() == []
    assert env.isolated_health == [(before, directory, previous)]
    assert_shared_restarted_once(env)
    assert_group_health(env, before, directory)
    for unit in ('proxyhub-hy2.service', *('proxyhub-node@' + node['id'] + '.service' for node in before['nodes'])):
        assert any(call[1] == 'stop' and unit in call for call in service_calls(env))
    assert (previous / 'hy2.json').is_file()
    assert all((previous / 'nodes' / (node['id'] + '.json')).is_file() for node in before['nodes'])


def test_ordinary_operation_does_not_silently_migrate_legacy_layout(node_environment):
    env = node_environment
    config = nodes.change(read_config(env.pointer['current']), 'nodes.update', {
        'node_id': env.retained_id, 'name': 'Still an independent legacy resource'})
    prepared = deployment.prepare(env.store, config, node_operation={'kind': 'update', 'node_id': env.retained_id})
    directory = env.root / 'revisions' / prepared['revision']
    assert not (directory / 'shared.json').exists()
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert not any(SHARED in call for call in env.calls)


def test_migration_rejects_configuration_changes_before_services(node_environment):
    env = node_environment
    config = read_config(env.pointer['current'])
    config['subscription']['name'] = 'Not a runtime-only change'
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.prepare(env.store, config, runtime_only=True)
    assert env.calls == [] and env.store.pending() == []


def test_failed_member_health_restores_legacy_topology_and_identity(node_environment, monkeypatch):
    env = node_environment
    previous = env.pointer['current']
    before = read_config(previous)
    prepared, candidate = prepare_runtime(env)
    checked = []
    original = deployment._wait_nodes_health
    def health(config, directory, identities=None):
        checked.append(directory)
        if directory == candidate:
            raise OperationError('A shared member did not authenticate')
        original(config, directory, identities)
    monkeypatch.setattr(deployment, '_wait_nodes_health', health)
    env.calls.clear()
    revision = env.store.revision
    with pytest.raises(OperationError, match='did not authenticate'):
        deployment.apply(env.store, prepared['plan_id'])
    assert checked == [candidate, previous]
    assert env.pointer['current'] == previous and read_config(previous) == before
    assert env.store.revision == revision and env.store.pending() == []
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed'
    assert_original_services_untouched(env)
    stopped = env.calls.index(('systemctl', 'stop', SHARED))
    legacy = {'proxyhub-hy2.service', *('proxyhub-node@' + node['id'] + '.service' for node in before['nodes'])}
    for unit in legacy:
        assert any(index > stopped and call[0:2] in {('systemctl', 'start'), ('systemctl', 'restart')}
                   and unit in call for index, call in enumerate(env.calls))


@pytest.mark.parametrize('stage', ['linked', 'published', 'shared-started'])
def test_process_interruption_recovers_old_topology_and_is_idempotent(node_environment, monkeypatch, stage):
    env = node_environment
    previous = env.pointer['current']
    before = read_config(previous)
    prepared, candidate = prepare_runtime(env)
    link, publish, run = deployment._link, deployment._publish, deployment.run
    def interrupted_link(directory):
        link(directory)
        if stage == 'linked' and directory == candidate:
            raise Interrupted()
    def interrupted_publish(directory):
        publish(directory)
        if stage == 'published' and directory == candidate:
            raise Interrupted()
    def interrupted_run(command, **kwargs):
        result = run(command, **kwargs)
        if stage == 'shared-started' and tuple(command[:2]) in {
                ('systemctl', 'start'), ('systemctl', 'restart')} and SHARED in command:
            raise Interrupted()
        return result
    monkeypatch.setattr(deployment, '_link', interrupted_link)
    monkeypatch.setattr(deployment, '_publish', interrupted_publish)
    monkeypatch.setattr(deployment, 'run', interrupted_run)
    with pytest.raises(Interrupted):
        deployment.apply(env.store, prepared['plan_id'])
    assert env.store.pending()
    monkeypatch.setattr(deployment, '_link', link)
    monkeypatch.setattr(deployment, '_publish', publish)
    monkeypatch.setattr(deployment, 'run', run)
    env.calls.clear()
    revision = env.store.revision
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == previous and read_config(previous) == before
    assert env.store.revision == revision and env.store.pending() == []
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed'
    assert_original_services_untouched(env)
    env.calls.clear()
    assert deployment.recover(env.store)['status'] == 'nothing_to_recover' and env.calls == []


def test_restore_failure_keeps_journal_for_retry(node_environment, monkeypatch):
    env = node_environment
    previous = env.pointer['current']
    prepared, _ = prepare_runtime(env)
    original = deployment._wait_nodes_health
    def unavailable(*args, **kwargs):
        raise OperationError('Shared member is unavailable')
    monkeypatch.setattr(deployment, '_wait_nodes_health', unavailable)
    with pytest.raises(OperationError):
        deployment.apply(env.store, prepared['plan_id'])
    assert env.store.pending()
    with pytest.raises(OperationError):
        deployment.recover(env.store)
    assert env.store.pending()
    monkeypatch.setattr(deployment, '_wait_nodes_health', original)
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == previous and env.store.pending() == []
    assert_original_services_untouched(env)


@pytest.mark.parametrize('layout', ['legacy', 'shared'])
@pytest.mark.parametrize('interrupted', [False, True])
def test_recovery_keeps_pending_until_real_bounded_wait_checks_every_member(node_environment, monkeypatch,
                                                                         layout, interrupted):
    env = node_environment
    _, certificate_id = add_node(env, protocol='anytls', name='Certificate member', port=5445,
                                 options={'sni': 'second.example.test', 'certificate_id': CERTIFICATE})
    previous = migrate(env) if layout == 'shared' else env.pointer['current']
    before = read_config(previous)
    if layout == 'legacy':
        prepared, candidate = prepare_runtime(env)
    else:
        changed = nodes.change(before, 'nodes.update', {'node_id': env.retained_id, 'port': 5543})
        prepared = deployment.prepare(env.store, changed,
                                      node_operation={'kind': 'update', 'node_id': env.retained_id})
        candidate = env.root / 'revisions' / prepared['revision']
    events, sleeps = [], []
    unavailable = True
    fingerprints = {previous / 'hy2.json': 'a' * 64, candidate / 'hy2.json': 'a' * 64,
                    previous / 'nodes' / (certificate_id + '.json'): 'b' * 64,
                    candidate / 'nodes' / (certificate_id + '.json'): 'b' * 64}
    def observe(member, fingerprint):
        assert env.store.pending(), 'No member check may acknowledge an unjournalled transition'
        events.append((env.pointer['current'], member, 'pki' if fingerprint is None else 'pin'))
    def hy2_probe(config, fingerprint=None):
        observe('HY2', fingerprint)
    def node_probe(config, outbound, fingerprint=None):
        observe(outbound['name'], fingerprint)
        if unavailable and outbound['name'] == 'Certificate member' and fingerprint is not None:
            raise OperationError('Certificate member remains unavailable')
    monkeypatch.setattr(deployment, '_wait_hy2_health', REAL_WAIT_HY2_HEALTH)
    monkeypatch.setattr(deployment, '_wait_nodes_health', REAL_WAIT_NODES_HEALTH)
    monkeypatch.setattr(deployment, '_hy2_probe', hy2_probe)
    monkeypatch.setattr(deployment, '_proxy_probe', node_probe)
    monkeypatch.setattr(deployment, '_runtime_fingerprint', fingerprints.__getitem__)
    monkeypatch.setattr(deployment, '_generated_client', lambda config: {'proxies': [
        {'name': node['name']} for node in config['nodes'] if node['enabled']]})
    monkeypatch.setattr(deployment.time, 'sleep', sleeps.append)
    revision = env.store.revision
    original_run = deployment.run
    if interrupted:
        def stopped(command, **kwargs):
            result = original_run(command, **kwargs)
            if tuple(command[:2]) in {('systemctl', 'start'), ('systemctl', 'restart')} and SHARED in command:
                raise Interrupted()
            return result
        monkeypatch.setattr(deployment, 'run', stopped)
        with pytest.raises(Interrupted):
            deployment.apply(env.store, prepared['plan_id'])
        monkeypatch.setattr(deployment, 'run', original_run)
    else:
        with pytest.raises(OperationError, match='Protocol node authentication'):
            deployment.apply(env.store, prepared['plan_id'])
    assert env.store.pending() and env.store.revision == revision
    events.clear()
    sleeps.clear()
    with pytest.raises(OperationError, match='Protocol node authentication'):
        deployment.recover(env.store)
    expected = [('HY2', 'pki'), ('HY2', 'pin'), ('Retained SS', 'pki')] + [
        ('Certificate member', stage) for _ in range(3) for stage in ('pki', 'pin')]
    assert events == [(previous, member, stage) for member, stage in expected]
    assert sleeps == [.2, .2]
    assert env.store.pending() and env.store.revision == revision
    unavailable = False
    events.clear()
    sleeps.clear()
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert events == [(previous, member, stage) for member, stage in expected[:5]]
    assert sleeps == [] and env.pointer['current'] == previous
    assert read_config(previous) == before and env.store.revision == revision
    assert env.store.pending() == [] and env.store.inspect(prepared['plan_id'])['status'] == 'failed'
    assert_original_services_untouched(env)
    env.calls.clear()
    events.clear()
    assert deployment.recover(env.store)['status'] == 'nothing_to_recover'
    assert env.calls == [] and events == []


@pytest.mark.parametrize('layout', ['legacy', 'shared'])
def test_transient_member_failure_completes_only_after_all_members_authenticate(node_environment, monkeypatch, layout):
    env = node_environment
    _, certificate_id = add_node(env, protocol='anytls', name='Certificate member', port=5445,
                                 options={'sni': 'second.example.test', 'certificate_id': CERTIFICATE})
    previous = migrate(env) if layout == 'shared' else env.pointer['current']
    before = read_config(previous)
    if layout == 'legacy':
        prepared, candidate = prepare_runtime(env)
    else:
        changed = nodes.change(before, 'nodes.update', {'node_id': env.retained_id, 'port': 5543})
        prepared = deployment.prepare(env.store, changed,
                                      node_operation={'kind': 'update', 'node_id': env.retained_id})
        candidate = env.root / 'revisions' / prepared['revision']
    events, sleeps = [], []
    attempt = 0
    def observe(member, fingerprint):
        assert env.store.pending()
        events.append((member, 'pki' if fingerprint is None else 'pin'))
    def hy2_probe(config, fingerprint=None):
        observe('HY2', fingerprint)
    def node_probe(config, outbound, fingerprint=None):
        nonlocal attempt
        observe(outbound['name'], fingerprint)
        if outbound['name'] == 'Certificate member':
            if fingerprint is None:
                attempt += 1
            elif attempt < 3:
                raise OperationError('Certificate member transient fixture failure')
    monkeypatch.setattr(deployment, '_wait_hy2_health', REAL_WAIT_HY2_HEALTH)
    monkeypatch.setattr(deployment, '_wait_nodes_health', REAL_WAIT_NODES_HEALTH)
    monkeypatch.setattr(deployment, '_hy2_probe', hy2_probe)
    monkeypatch.setattr(deployment, '_proxy_probe', node_probe)
    monkeypatch.setattr(deployment, '_runtime_fingerprint', lambda path: 'c' * 64)
    monkeypatch.setattr(deployment, '_generated_client', lambda config: {'proxies': [
        {'name': node['name']} for node in config['nodes'] if node['enabled']]})
    monkeypatch.setattr(deployment.time, 'sleep', sleeps.append)
    assert deployment.apply(env.store, prepared['plan_id'])['status'] == 'applied'
    assert events == [('HY2', 'pki'), ('HY2', 'pin'), ('Retained SS', 'pki')] + [
        ('Certificate member', stage) for _ in range(3) for stage in ('pki', 'pin')]
    assert sleeps == [.2, .2] and env.pointer['current'] == candidate
    assert env.store.pending() == [] and env.store.inspect(prepared['plan_id'])['status'] == 'applied'
    assert_original_services_untouched(env)


@pytest.mark.parametrize('mutation', ['changed', 'missing', 'manifest', 'symlink'])
def test_shared_candidate_integrity_precedes_any_service_change(node_environment, mutation):
    env = node_environment
    previous = env.pointer['current']
    prepared, directory = prepare_runtime(env)
    path = directory / 'shared.json'
    identity = prepared['plan_id']
    if mutation == 'changed':
        path.write_text('{}')
    elif mutation == 'missing':
        path.unlink()
    elif mutation == 'manifest':
        payload = env.store.inspect(identity)['payload']
        payload['files'].pop('shared.json')
        identity = env.store.plan('deployment', payload)
    else:
        path.unlink()
        try:
            path.symlink_to(directory / 'hy2.json')
        except OSError:
            pytest.skip('Creating symlinks is unavailable on this host')
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.apply(env.store, identity)
    assert env.calls == [] and env.pointer['current'] == previous and env.store.pending() == []


@pytest.mark.parametrize('resource', ['hy2', 'node', 'subscription'])
def test_shared_metadata_changes_publish_without_restarting_members(node_environment, resource):
    env = node_environment
    identity = env.retained_id
    if resource == 'node':
        # Unlike SS, VLESS carries an optional user name in its server runtime.
        _, identity = add_node(env, protocol='vless', name='Display node', port=5544,
                               options={'tls': False})
    previous = migrate(env)
    before = read_config(previous)
    if resource == 'hy2':
        config = hy2.change(before, 'hy2.update', {'name': 'Renamed HY2'})
        options = {'hy2_operation': {'kind': 'update'}}
    elif resource == 'node':
        config = nodes.change(before, 'nodes.update', {'node_id': identity, 'name': 'Renamed node'})
        options = {'node_operation': {'kind': 'update', 'node_id': identity}}
    else:
        config = deepcopy(before)
        config['subscription']['name'] = 'Renamed subscription'
        options = {'publish_only': True}
    prepared = deployment.prepare(env.store, config, **options)
    directory = env.root / 'revisions' / prepared['revision']
    assert (directory / 'shared.json').read_bytes() == (previous / 'shared.json').read_bytes()
    assert prepared['shared_runtime']['changed'] is False
    assert prepared['shared_runtime']['reconnect_required'] is False
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert service_calls(env) == [('systemctl', 'reload', 'proxyhub-publisher.service')]
    assert not env.hy2_health and not env.node_health


def test_shared_node_change_restarts_once_and_authenticates_unchanged_sibling(node_environment):
    env = node_environment
    previous = migrate(env)
    before = read_config(previous)
    config = nodes.change(before, 'nodes.update', {'node_id': env.retained_id, 'port': 5543})
    prepared = deployment.prepare(env.store, config, node_operation={'kind': 'update', 'node_id': env.retained_id})
    directory = env.root / 'revisions' / prepared['revision']
    assert prepared['shared_runtime']['changed'] is True
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert_shared_restarted_once(env)
    assert_group_health(env, config, directory)
    assert read_config(directory)['hy2'] == before['hy2']
    assert nodes.get_node(config['nodes'], env.retained_id)['credentials'] == nodes.get_node(before['nodes'], env.retained_id)['credentials']
    assert not any(any(name.startswith(('proxyhub-node@', 'proxyhub-hy2.')) for name in call[2:])
                   for call in service_calls(env))


def test_full_server_display_name_change_keeps_shared_process(node_environment):
    env = node_environment
    previous = migrate(env)
    config = read_config(previous)
    config['server']['node_name'] = 'Renamed primary REALITY'
    prepared = deployment.prepare(env.store, config)
    directory = env.root / 'revisions' / prepared['revision']
    assert (directory / 'shared.json').read_bytes() == (previous / 'shared.json').read_bytes()
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert not any(SHARED in call and call[1] in {'start', 'restart', 'stop'} for call in service_calls(env))
    assert read_config(directory)['server']['node_name'] == config['server']['node_name']


@pytest.mark.parametrize('material_changed', [False, True])
def test_full_failure_restores_shared_group_once_only_if_runtime_changed(node_environment, monkeypatch, material_changed):
    env = node_environment
    previous = migrate(env)
    before = read_config(previous)
    config = deepcopy(before)
    config['server']['node_name'] = 'Candidate primary REALITY'
    if material_changed:
        monkeypatch.setattr(deployment, '_certificate_material', lambda path:
                            ['REPLACED-KEY'] if path.endswith('privkey.pem') else ['REPLACED-CERTIFICATE'])
    prepared = deployment.prepare(env.store, config)
    candidate = env.root / 'revisions' / prepared['revision']
    original_run = deployment.run
    group_starts = []
    def observe_run(command, **kwargs):
        if SHARED in command and command[1] in {'start', 'restart'}:
            group_starts.append(env.pointer['current'])
        return original_run(command, **kwargs)
    def web_health(snapshot, directory):
        if directory == candidate:
            raise OperationError('Full candidate website did not acknowledge')
    monkeypatch.setattr(deployment, 'run', observe_run)
    monkeypatch.setattr(deployment, '_wait_web_health', web_health)
    env.calls.clear()
    with pytest.raises(OperationError, match='did not acknowledge'):
        deployment.apply(env.store, prepared['plan_id'])
    assert group_starts == ([candidate, previous] if material_changed else [])
    assert env.pointer['current'] == previous and read_config(previous) == before
    assert env.store.pending() == [] and env.store.inspect(prepared['plan_id'])['status'] == 'failed'


@pytest.mark.parametrize('interrupted', [False, True])
def test_shared_update_failure_restores_previous_group_and_checks_all_members(node_environment, monkeypatch, interrupted):
    env = node_environment
    previous = migrate(env)
    before = read_config(previous)
    runtime = (previous / 'shared.json').read_bytes()
    config = nodes.change(before, 'nodes.update', {'node_id': env.retained_id, 'port': 5543})
    prepared = deployment.prepare(env.store, config, node_operation={'kind': 'update', 'node_id': env.retained_id})
    candidate = env.root / 'revisions' / prepared['revision']
    revision = env.store.revision
    if interrupted:
        original = deployment.run
        def stopped(command, **kwargs):
            result = original(command, **kwargs)
            if tuple(command[:2]) in {('systemctl', 'start'), ('systemctl', 'restart')} and SHARED in command:
                raise Interrupted()
            return result
        monkeypatch.setattr(deployment, 'run', stopped)
        with pytest.raises(Interrupted):
            deployment.apply(env.store, prepared['plan_id'])
        assert env.store.pending()
        monkeypatch.setattr(deployment, 'run', original)
        assert deployment.recover(env.store)['status'] == 'recovered'
    else:
        original = deployment._wait_nodes_health
        def health(snapshot, directory, identities=None):
            if directory == candidate:
                raise OperationError('Updated member did not authenticate')
            original(snapshot, directory, identities)
        monkeypatch.setattr(deployment, '_wait_nodes_health', health)
        with pytest.raises(OperationError, match='did not authenticate'):
            deployment.apply(env.store, prepared['plan_id'])
    assert env.pointer['current'] == previous and read_config(previous) == before
    assert (previous / 'shared.json').read_bytes() == runtime
    assert env.store.revision == revision and env.store.pending() == []
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed'
    assert_group_health(env, before, previous)
    assert_original_services_untouched(env)
    assert not any(any(name.startswith(('proxyhub-node@', 'proxyhub-hy2.')) for name in call[2:])
                   for call in service_calls(env))


def test_disabling_last_member_stops_group_but_keeps_identity_and_can_enable(node_environment):
    env = node_environment
    migrate(env)
    before = read_config(env.pointer['current'])
    config = hy2.change(before, 'hy2.disable', {})
    prepared = deployment.prepare(env.store, config, hy2_operation={'kind': 'disable'})
    deployment.apply(env.store, prepared['plan_id'])
    config = nodes.change(read_config(env.pointer['current']), 'nodes.disable', {'node_id': env.retained_id})
    prepared = deployment.prepare(env.store, config, node_operation={'kind': 'disable', 'node_id': env.retained_id})
    directory = env.root / 'revisions' / prepared['revision']
    assert json.loads((directory / 'shared.json').read_text())['inbounds'] == []
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert ('systemctl', 'stop', SHARED) in env.calls
    assert not any(SHARED in call and call[1] in {'start', 'restart'} for call in service_calls(env))
    stopped = read_config(directory)
    assert stopped['hy2']['password'] == before['hy2']['password']
    assert nodes.get_node(stopped['nodes'], env.retained_id)['credentials'] == nodes.get_node(before['nodes'], env.retained_id)['credentials']
    config = nodes.change(stopped, 'nodes.enable', {'node_id': env.retained_id})
    prepared = deployment.prepare(env.store, config, node_operation={'kind': 'enable', 'node_id': env.retained_id})
    env.calls.clear()
    deployment.apply(env.store, prepared['plan_id'])
    assert_shared_restarted_once(env)
