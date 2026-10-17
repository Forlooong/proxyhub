"""Transaction boundaries and recovery; no privileged services are touched."""
from contextlib import nullcontext
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxyhub.core import OperationError, canonical, digest
from proxyhub.state import StateStore
from proxyhub import deployment
from proxyhub.proxy import validate_spec
from proxyhub.publication import save_security


def configuration(node_name='Test node'):
    return {'server':validate_spec({
        'certificate_path':'/var/lib/proxyhub/certificates/0123456789abcdef/fullchain.pem',
        'key_path':'/var/lib/proxyhub/certificates/0123456789abcdef/privkey.pem',
        'site_domain':'site.example.test','subscription_domain':'sub.example.test',
        'public_address':'203.0.113.10','uuid':'a875d3ac-980f-4a0c-929e-78cc678429f1',
        'private_key':base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='),
        'public_key':base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode().rstrip('='),
        'short_id':'0123456789abcdef','node_name':node_name}),
        'subscription':{'id':'default','token':'T'*43,'name':'Test subscription',
                        'userinfo':{},'update_interval_hours':6},'overrides':{}}


@pytest.fixture
def store(tmp_path):
    value = StateStore(tmp_path / 'plans')
    yield value
    value.db.close()


def test_commit_is_atomic_and_single_use(store):
    identity = store.plan('ssh', {'key': 'public'})
    store.commit(identity)
    assert store.revision == 1
    assert store.inspect(identity)['status'] == 'applied'
    assert store.db.execute('SELECT stage FROM journal').fetchall() == [('committed',)]
    with pytest.raises(OperationError):
        store.commit(identity)
    with pytest.raises(OperationError):
        store.commit('missing')
    assert store.revision == 1


def test_stale_plan_cannot_commit(store):
    first = store.plan('ssh', {})
    second = store.plan('ssh', {})
    store.commit(first)
    with pytest.raises(OperationError):
        store.commit(second)
    assert store.revision == 1


def test_prepare_does_not_bind_old_work_to_new_revision(store):
    before = store.revision
    change = store.plan('ssh', {})
    store.commit(change)
    with pytest.raises(OperationError, match='changed during preparation'):
        store.plan('deployment', {'revision':'a'*32}, expected_revision=before)


def test_pending_activation_blocks_new_work_and_survives_reopen(store):
    identity = store.plan('deployment', {'revision': 'a' * 32})
    other = store.plan('ssh', {})
    store.record(identity, 'activating', {'previous': None, 'candidate': '/managed'})
    with pytest.raises(OperationError):
        store.plan('ssh', {})
    with pytest.raises(OperationError):
        store.get(other)
    with pytest.raises(OperationError):
        store.commit(other)
    reopened = StateStore(store.root)
    try:
        assert reopened.pending()[0]['plan_id'] == identity
    finally:
        reopened.db.close()
    store.abort(identity, {'recovered': True})
    assert store.pending() == []
    assert store.inspect(identity)['status'] == 'failed'
    assert store.get(other) == ('ssh', {})


def test_commit_closes_activation_in_same_transaction(store):
    identity = store.plan('deployment', {})
    store.record(identity, 'activating', {'previous': None, 'candidate': '/managed'})
    store.commit(identity)
    assert store.pending() == []
    assert store.revision == 1


def test_plan_integrity_is_checked_at_commit(store):
    identity = store.plan('ssh', {})
    with store.db:
        store.db.execute("UPDATE plans SET payload='{}' WHERE id=?", (identity,))
    with pytest.raises(OperationError, match='integrity'):
        store.commit(identity)
    assert store.revision == 0


def test_deployment_cannot_commit_without_activation(store):
    identity = store.plan('deployment', {})
    with pytest.raises(OperationError, match='pending activation'):
        store.commit(identity)
    assert store.revision == 0


@pytest.fixture
def activation(tmp_path, monkeypatch, store):
    root = tmp_path / 'runtime'
    (root / 'revisions').mkdir(parents=True)
    (root / 'publish').mkdir()
    monkeypatch.setattr(deployment, 'ROOT', root)
    monkeypatch.setitem(sys.modules,'pwd',SimpleNamespace(getpwnam=lambda name:SimpleNamespace(pw_gid=123)))
    monkeypatch.setattr(deployment.os,'chown',lambda *args:None,raising=False)
    save_security(root/'publish/security.json',{'schema_version':1,'generation':0,
                                              'revoked_tokens':[],'retired_aliases':[]})
    monkeypatch.setattr(store, 'exclusive', nullcontext)
    pointer = {'current': None}
    calls = []
    monkeypatch.setattr(deployment, '_current_revision', lambda: pointer['current'])
    monkeypatch.setattr(deployment, '_link', lambda value: pointer.update(current=value))
    monkeypatch.setattr(deployment, '_clear_current', lambda: pointer.update(current=None))
    monkeypatch.setattr(deployment, '_publish', lambda value: calls.append(('publish', value)))
    monkeypatch.setattr(deployment, '_check_ports', lambda server: None)
    monkeypatch.setattr(deployment, '_health', lambda server: None)
    monkeypatch.setattr(deployment, '_wait_publication',lambda directory:None)
    monkeypatch.setattr(deployment, 'run', lambda command: calls.append(tuple(command)))
    monkeypatch.setattr(deployment.time, 'sleep', lambda value: None)
    monkeypatch.setattr(deployment, 'validate_certificate', lambda *a: {'sha256': 'c' * 64})
    certificate = root / 'cert.pem'
    key = root / 'key.pem'
    certificate.write_bytes(b'certificate test fixture')
    key.write_bytes(b'key test fixture')
    monkeypatch.setattr(deployment,'_certificate_files',lambda server:{
        'certificate_path':digest(certificate.read_bytes()),'key_path':digest(key.read_bytes())})

    def candidate(char='b'):
        directory = root / 'revisions' / (char * 32)
        directory.mkdir()
        (directory / 'site').mkdir()
        for name in deployment.CANDIDATE_FILES:
            (directory / name).write_bytes(canonical(configuration()) if name == 'config.json' else b'fixture')
        payload = {'revision': directory.name,
                   'files': {name: digest((directory / name).read_bytes()) for name in deployment.CANDIDATE_FILES},
                   'certificate_sha256': 'c' * 64,
                   'certificate_files': {'certificate_path': digest(certificate.read_bytes()), 'key_path': digest(key.read_bytes())}}
        return directory, store.plan('deployment', payload)
    return root, pointer, calls, candidate, certificate


def test_successful_activation_commits_once(store, activation):
    root, pointer, calls, candidate, _ = activation
    directory, identity = candidate()
    result = deployment.apply(store, identity)
    assert result['status'] == 'applied'
    assert pointer['current'] == directory
    assert store.revision == 1 and store.pending() == []
    assert deployment.recover(store)['status'] == 'nothing_to_recover'


@pytest.mark.parametrize('mutation', ['candidate', 'certificate'])
def test_changed_files_refused_before_activation(store, activation, mutation):
    root, pointer, calls, candidate, certificate = activation
    directory, identity = candidate()
    (directory / 'nginx.conf' if mutation == 'candidate' else certificate).write_bytes(b'changed')
    with pytest.raises(OperationError, match='changed'):
        deployment.apply(store, identity)
    assert calls == [] and pointer['current'] is None
    assert store.pending() == [] and store.revision == 0


def test_renewed_certificate_fingerprint_refused(store, activation, monkeypatch):
    *_, candidate, _ = activation
    _, identity = candidate()
    monkeypatch.setattr(deployment, 'validate_certificate', lambda *a: {'sha256': 'd' * 64})
    with pytest.raises(OperationError, match='certificate'):
        deployment.apply(store, identity)
    assert store.pending() == []


def test_runtime_failure_restores_previous_bundle_and_services(store, activation, monkeypatch):
    root, pointer, calls, candidate, _ = activation
    old = root / 'revisions' / ('a' * 32)
    old.mkdir()
    (old / 'config.json').write_bytes(canonical(configuration('Previous node')))
    pointer['current'] = old
    directory, identity = candidate()
    health_calls = []
    def health(server):
        health_calls.append(server)
        if len(health_calls) < 18:
            raise OperationError('health failed')
    monkeypatch.setattr(deployment, '_health', health)
    with pytest.raises(OperationError, match='health failed'):
        deployment.apply(store, identity)
    assert pointer['current'] == old
    assert ('publish', old) in calls
    assert store.inspect(identity)['status'] == 'failed'
    assert store.revision == 0 and store.pending() == []
    assert len(health_calls) == 18
    assert health_calls[-3:] == [configuration('Previous node')['server']] * 3


def test_first_activation_failure_removes_publication(store, activation, monkeypatch):
    root, pointer, calls, candidate, _ = activation
    _, identity = candidate()
    (root / 'publish/bundle.json').write_bytes(b'old publication')
    monkeypatch.setattr(deployment, '_health', lambda server: (_ for _ in ()).throw(OperationError('failed')))
    with pytest.raises(OperationError):
        deployment.apply(store, identity)
    assert pointer['current'] is None
    assert not (root / 'publish/bundle.json').exists()
    assert ('systemctl', 'disable', *deployment._units()) in calls


def test_interrupted_activation_is_rolled_back_idempotently(store, activation):
    root, pointer, calls, candidate, _ = activation
    old = root / 'revisions' / ('a' * 32)
    old.mkdir()
    (old / 'config.json').write_bytes(canonical(configuration('Previous node')))
    directory, identity = candidate()
    pointer['current'] = directory
    store.record(identity, 'activating', {'previous': str(old), 'candidate': str(directory)})
    assert deployment.recover(store)['status'] == 'recovered'
    assert pointer['current'] == old and ('publish', old) in calls
    assert store.inspect(identity)['status'] == 'failed'
    assert deployment.recover(store)['status'] == 'nothing_to_recover'


def test_rollback_health_failure_keeps_activation_pending(store, activation, monkeypatch):
    root, pointer, calls, candidate, _ = activation
    old = root / 'revisions' / ('a' * 32)
    old.mkdir()
    (old / 'config.json').write_bytes(canonical(configuration('Previous node')))
    pointer['current'] = old
    directory, identity = candidate()
    probes = []
    def failing_health(server):
        probes.append(server)
        raise OperationError('HTTPS still unavailable')
    monkeypatch.setattr(deployment, '_health', failing_health)
    with pytest.raises(OperationError, match='HTTPS still unavailable'):
        deployment.apply(store, identity)
    assert len(probes) == 30
    assert pointer['current'] == old
    assert store.inspect(identity)['status'] == 'prepared'
    assert len(store.pending()) == 1
    with pytest.raises(OperationError, match='HTTPS still unavailable'):
        deployment.recover(store)
    assert len(probes) == 45 and len(store.pending()) == 1
    assert store.revision == 0
    monkeypatch.setattr(deployment, '_health', lambda server: None)
    assert deployment.recover(store)['status'] == 'recovered'
    assert store.pending() == []


def test_recovery_rejects_external_previous_path(store, activation):
    root, pointer, calls, candidate, _ = activation
    directory, identity = candidate()
    store.record(identity, 'activating', {'previous': str(root.parent), 'candidate': str(directory)})
    with pytest.raises(OperationError, match='managed'):
        deployment.recover(store)
    assert calls == [] and len(store.pending()) == 1


def test_failed_recovery_stays_pending_for_retry(store, activation, monkeypatch):
    root, pointer, calls, candidate, _ = activation
    directory, identity = candidate()
    pointer['current'] = directory
    store.record(identity, 'activating', {'previous': None, 'candidate': str(directory)})
    monkeypatch.setattr(deployment, 'run', lambda args: (_ for _ in ()).throw(OperationError('service failed')))
    with pytest.raises(OperationError):
        deployment.recover(store)
    assert len(store.pending()) == 1
    with pytest.raises(OperationError):
        store.plan('ssh', {})


def test_recovery_never_overwrites_unrelated_current_revision(store, activation):
    root, pointer, calls, candidate, _ = activation
    directory, identity = candidate()
    other = root / 'revisions' / ('f' * 32)
    other.mkdir()
    pointer['current'] = other
    store.record(identity, 'activating', {'previous': None, 'candidate': str(directory)})
    with pytest.raises(OperationError, match='current'):
        deployment.recover(store)
    assert calls == [] and pointer['current'] == other


def test_manifest_path_escape_is_rejected_before_any_service_change(store, activation):
    root, pointer, calls, candidate, _ = activation
    directory, identity = candidate()
    payload = store.inspect(identity)['payload']
    payload['files']['../outside'] = '0' * 64
    replacement = store.plan('deployment', payload)
    with pytest.raises(OperationError, match='manifest'):
        deployment.apply(store, replacement)
    assert calls == []


def test_revision_traversal_is_rejected(activation):
    root, *_ = activation
    with pytest.raises(OperationError, match='managed'):
        deployment._managed_revision(root / 'revisions/../outside')


def test_renewal_timer_does_not_join_activation_services():
    from proxyhub.installer import UNITS, units
    definitions = units()
    assert 'proxyhub-renew.service' not in UNITS
    assert 'proxyhub-renew.timer' not in UNITS
    assert 'ExecStart=/usr/local/bin/proxyhub deployment renew' in definitions['proxyhub-renew.service']
    assert 'ConditionPathExists=/var/lib/proxyhub/current/config.json' in definitions['proxyhub-renew.service']
    assert 'Persistent=true' in definitions['proxyhub-renew.timer']
    assert 'ExecStartPre=+/usr/local/bin/proxyhub deployment check-publication\n' in definitions['proxyhub-publisher.service']


def test_reality_instance_template_is_isolated_from_legacy_activation():
    from proxyhub.installer import UNITS, units
    definitions = units()
    legacy = definitions['proxyhub-proxy.service']
    template = definitions['proxyhub-reality@.service']
    assert template == legacy.replace('/current/sing-box.json', '/current/reality/%i.json')
    assert template.startswith('# Managed by ProxyHub\n')
    assert 'User=proxyhub-proxy\nGroup=proxyhub-proxy\n' in template
    assert 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK\n' in template
    assert 'proxyhub-reality@.service' not in UNITS
    assert 'ExecStart=/opt/proxyhub/bin/sing-box run -c /var/lib/proxyhub/current/sing-box.json\n' in legacy


@pytest.mark.parametrize(('unit', 'config'), [
    ('proxyhub-shared.service', 'shared.json'),
    ('proxyhub-hy2.service', 'hy2.json'),
    ('proxyhub-node@.service', 'nodes/%i.json'),
])
def test_extra_proxy_service_is_low_privilege_and_isolated_from_legacy_activation(unit, config):
    from proxyhub.installer import UNITS, units
    definition = units()[unit]
    assert UNITS == ('proxyhub-proxy.service', 'proxyhub-publisher.service', 'proxyhub-nginx.service')
    assert unit not in UNITS
    assert definition.startswith('# Managed by ProxyHub\n')
    assert 'User=proxyhub-proxy\nGroup=proxyhub-proxy\n' in definition
    assert f'ExecStart=/opt/proxyhub/bin/sing-box run -c /var/lib/proxyhub/current/{config}\n' in definition
    assert 'CapabilityBoundingSet=CAP_NET_BIND_SERVICE\nAmbientCapabilities=CAP_NET_BIND_SERVICE\n' in definition
    assert 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK\n' in definition
    for restriction in ('NoNewPrivileges=yes', 'PrivateTmp=yes', 'ProtectHome=yes', 'ProtectSystem=strict'):
        assert restriction in definition
