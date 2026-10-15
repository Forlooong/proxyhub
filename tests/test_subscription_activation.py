"""Publication transactions keep proxy and Nginx running and require real ACKs."""
from contextlib import nullcontext
import base64
import json
import os
import stat
import sys
import threading
from types import SimpleNamespace

import pytest

from proxyhub import deployment
from proxyhub.core import OperationError, canonical, digest
from proxyhub.publication import build_bundle, save_bundle
from proxyhub.proxy import validate_spec
from proxyhub.publisher import PublisherServer
from proxyhub.state import StateStore


def server_spec(**changes):
    value = {
        'site_domain': 'site.example.test', 'subscription_domain': 'sub.example.test',
        'public_address': '203.0.113.10', 'uuid': 'a875d3ac-980f-4a0c-929e-78cc678429f1',
        'private_key': base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='),
        'public_key': base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode().rstrip('='),
        'short_id': '0123456789abcdef',
        'certificate_path': '/var/lib/proxyhub/certificates/0123456789abcdef/fullchain.pem',
        'key_path': '/var/lib/proxyhub/certificates/0123456789abcdef/privkey.pem',
    }
    value.update(changes)
    return validate_spec(value)


def profile(**changes):
    value = {'id': 'default', 'host': 'sub.example.test', 'token': 'T' * 43,
             'name': 'Test subscription', 'yaml': 'mode: rule\n',
             'raw': base64.b64encode(b'vless://public-test-fixture').decode(),
             'userinfo': {}, 'update_interval_hours': 6}
    value.update(changes)
    return value


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    (root / 'revisions').mkdir(parents=True)
    (root / 'publish').mkdir()
    store = StateStore(tmp_path / 'plans')
    monkeypatch.setattr(store, 'exclusive', nullcontext)
    monkeypatch.setattr(deployment, 'ROOT', root)
    pointer = {'current': None}
    calls = []
    monkeypatch.setattr(deployment, '_current_revision', lambda: pointer['current'])
    monkeypatch.setattr(deployment, '_link', lambda path: pointer.update(current=path))
    monkeypatch.setattr(deployment, '_publish', lambda path: calls.append(('publish', path)))
    monkeypatch.setattr(deployment, 'run', lambda command, **kwargs: calls.append(tuple(command)))
    monkeypatch.setattr(deployment, 'validate_certificate', lambda *a: {'sha256': 'c' * 64})
    monkeypatch.setattr(deployment, '_certificate_files', lambda server: {'certificate': 'c', 'key': 'k'})
    monkeypatch.setattr(deployment.time, 'sleep', lambda delay: None)

    def revision(char, *, server=None, publication_name='Test subscription'):
        directory = root / 'revisions' / (char * 32)
        (directory / 'site').mkdir(parents=True)
        metadata = profile(name=publication_name)
        config = {'server': server or server_spec(), 'subscription': {
            key: value for key, value in metadata.items() if key not in ('host', 'yaml', 'raw')
        }, 'overrides': {}}
        for name in deployment.CANDIDATE_FILES:
            (directory / name).write_bytes(b'test fixture')
        (directory / 'config.json').write_bytes(canonical(config))
        save_bundle(directory / 'bundle.json', build_bundle([metadata], directory.name))
        return directory

    previous = revision('a')
    pointer['current'] = previous

    def plan(char='b', *, activation='publication', server=None):
        directory = revision(char, server=server, publication_name='Updated subscription')
        payload = {'revision': directory.name, 'activation': activation,
                   'files': {name: digest((directory / name).read_bytes())
                             for name in deployment.CANDIDATE_FILES},
                   'certificate_sha256': 'c' * 64,
                   'certificate_files': {'certificate': 'c', 'key': 'k'}}
        identity = store.plan('deployment', payload)
        return directory, identity

    yield SimpleNamespace(root=root, store=store, pointer=pointer, calls=calls,
                          previous=previous, revision=revision, plan=plan)
    store.db.close()


def read_config(path):
    return json.loads((path / 'config.json').read_text(encoding='utf-8'))


@pytest.fixture
def website_environment(environment, monkeypatch):
    env = environment
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_gid=123)))
    monkeypatch.setattr(deployment.os, 'chown', lambda *a: None, raising=False)
    monkeypatch.setattr(deployment, 'render_nginx', lambda *a: 'test nginx config')
    monkeypatch.setattr(deployment, 'client_config', lambda *a: {'yaml':profile()['yaml'],'raw':profile()['raw']})
    monkeypatch.setattr(deployment, '_wait_health', lambda server: None)
    monkeypatch.setattr(deployment, '_wait_publication', lambda directory: None)
    monkeypatch.setattr(deployment, '_website_health', lambda server, directory: None)
    monkeypatch.setattr(deployment, '_wait_web_health', lambda config, directory: None)
    return env


def test_uploaded_content_survives_publication_and_restores_without_old_config(website_environment):
    env = website_environment
    config = read_config(env.previous)
    files = {'index.html':b'<h1>Uploaded</h1>','assets/site.css':b'h1 {color:blue}'}
    uploaded = deployment.prepare(env.store,config,site_files=files,website_change=True)
    with pytest.raises(OperationError,match='not been successfully applied'):
        deployment.content_version(env.store,uploaded['revision'])
    deployment.apply(env.store,uploaded['plan_id'])
    assert deployment.content_version(env.store,uploaded['revision']) == files
    config = read_config(env.pointer['current'])
    assert config['website_content'] == uploaded['revision']
    config['subscription']['name'] = 'New metadata'
    published = deployment.prepare(env.store,config,publish_only=True)
    env.calls.clear()
    deployment.apply(env.store,published['plan_id'])
    assert ('systemctl','reload','proxyhub-publisher.service') in env.calls
    assert not any('restart' in call for call in env.calls)
    assert (env.pointer['current']/'site/assets/site.css').read_bytes() == files['assets/site.css']
    versions = deployment.content_versions(env.store,config)
    assert [item['id'] for item in versions['versions']] == ['template',uploaded['revision']]
    assert versions['versions'][1]['current'] is True
    config['server']['node_name'] = 'Current node name'
    config.pop('website_content')
    config['web']['sites'][0]['content_version'] = None
    template = deployment.prepare(env.store,config,website_change=True)
    deployment.apply(env.store,template['plan_id'])
    assert 'website_content' not in read_config(env.pointer['current'])
    config['website_content'] = uploaded['revision']
    config['web']['sites'][0]['content_version'] = uploaded['revision']
    restored = deployment.prepare(env.store,config,website_change=True)
    deployment.apply(env.store,restored['plan_id'])
    active = read_config(env.pointer['current'])
    assert active['server']['node_name'] == 'Current node name'
    assert active['subscription']['name'] == 'New metadata'
    assert (env.pointer['current']/'site/index.html').read_bytes() == files['index.html']


@pytest.mark.parametrize('mutation',['asset','extra','missing'])
def test_uploaded_asset_changes_rejected_before_activation(website_environment,mutation):
    env = website_environment
    result = deployment.prepare(env.store,read_config(env.previous),site_files={
        'index.html':b'Upload','assets/style.css':b'body {}'},website_change=True)
    directory = env.root/'revisions'/result['revision']
    if mutation == 'asset':
        (directory/'site/assets/style.css').write_bytes(b'tampered')
    elif mutation == 'extra':
        (directory/'site/extra.txt').write_bytes(b'unlisted')
    else:
        (directory/'site/assets/style.css').unlink()
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.apply(env.store,result['plan_id'])
    assert env.calls == [] and env.pointer['current'] == env.previous


def test_website_ack_failure_restores_previous_and_does_not_publish_history(website_environment,monkeypatch):
    env = website_environment
    result = deployment.prepare(env.store,read_config(env.previous),site_files={'index.html':b'new'},website_change=True)
    def failure(*args):
        raise OperationError('Website has not loaded the selected content')
    monkeypatch.setattr(deployment,'_wait_web_health',failure)
    with pytest.raises(OperationError,match='not loaded'):
        deployment.apply(env.store,result['plan_id'])
    assert env.pointer['current'] == env.previous and env.store.pending() == []
    assert env.store.inspect(result['plan_id'])['status'] == 'failed'
    assert len(deployment.content_versions(env.store,read_config(env.previous))['versions']) == 1


def test_interrupted_website_activation_recovers_previous(website_environment):
    env = website_environment
    result = deployment.prepare(env.store,read_config(env.previous),site_files={'index.html':b'new','a/x.css':b'x {}'},website_change=True)
    directory = env.root/'revisions'/result['revision']
    env.store.record(result['plan_id'],'activating',{'previous':str(env.previous),'candidate':str(directory),'activation':'full'})
    env.pointer['current'] = directory
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == env.previous
    with pytest.raises(OperationError,match='not been successfully applied'):
        deployment.content_version(env.store,result['revision'])


def test_failed_website_prepare_cleans_only_its_candidate(website_environment,monkeypatch):
    env = website_environment
    before = set((env.root/'revisions').iterdir())
    def fail(*args,**kwargs):
        raise OperationError('runtime check failed')
    monkeypatch.setattr(deployment,'run',fail)
    with pytest.raises(OperationError,match='runtime check failed'):
        deployment.prepare(env.store,read_config(env.previous),site_files={'index.html':b'new'})
    assert set((env.root/'revisions').iterdir()) == before
    assert env.store.db.execute('SELECT count(*) FROM plans').fetchone()[0] == 0


def test_publication_cannot_restore_content(website_environment):
    env = website_environment
    config = read_config(env.previous)
    config['website_content'] = 'b'*32
    with pytest.raises(OperationError,match='cannot change website'):
        deployment.prepare(env.store,config,publish_only=True)


@pytest.mark.skipif(os.name != 'posix',reason='Real Unix directory modes')
def test_nested_uploaded_assets_keep_runtime_access_under_private_umask(website_environment):
    env = website_environment
    previous = os.umask(0o077)
    try:
        result = deployment.prepare(env.store,read_config(env.previous),site_files={'index.html':b'new','a/b/style.css':b'x {}'})
    finally:
        os.umask(previous)
    directory = env.root/'revisions'/result['revision']
    deployment._check_runtime_permissions(directory)
    assert stat.S_IMODE((directory/'site/a/b').stat().st_mode) == 0o755
    assert stat.S_IMODE((directory/'site/a/b/style.css').stat().st_mode) == 0o644


@pytest.mark.parametrize('field,value', [
    ('site_title', 'Changed title'), ('node_name', 'Changed node'),
    ('site_domain', 'new.example.test'), ('subscription_domain', 'newsub.example.test'),
    ('public_address', '203.0.113.11'), ('public_port', 4443), ('reality_port', 2444),
    ('uuid', 'b875d3ac-980f-4a0c-929e-78cc678429f1'), ('short_id', 'abcd'),
    ('private_key', base64.urlsafe_b64encode(bytes(32)).decode().rstrip('=')),
    ('public_key', base64.urlsafe_b64encode(bytes(32)).decode().rstrip('=')),
])
def test_prepare_publication_rejects_every_server_change(environment, field, value):
    env = environment
    config = read_config(env.previous)
    config['server'][field] = value
    with pytest.raises(OperationError, match='cannot change server'):
        deployment.prepare(env.store, config, publish_only=True)
    assert env.calls == [] and env.store.revision == 0


def test_prepare_publication_requires_active_deployment(environment):
    env = environment
    config = read_config(env.previous)
    env.pointer['current'] = None
    with pytest.raises(OperationError, match='requires an active'):
        deployment.prepare(env.store, config, publish_only=True)
    assert env.calls == []


def test_prepare_normalizes_server_and_records_publication_mode(environment, monkeypatch):
    env = environment
    config = read_config(env.previous)
    config['server'].pop('site_title')  # Same normalized default is permitted.
    config['subscription']['name'] = 'Updated subscription'
    config['overrides'] = {'mixed-port': 17891}
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_gid=123)))
    monkeypatch.setattr(deployment.os, 'chown', lambda *a: None, raising=False)
    monkeypatch.setattr(deployment, 'render_nginx', lambda *a: 'test nginx config')
    monkeypatch.setattr(deployment, 'client_config', lambda *a: {
        'yaml': profile()['yaml'], 'raw': profile()['raw'],
    })
    result = deployment.prepare(env.store, config, expected_revision=0, publish_only=True)
    payload = env.store.inspect(result['plan_id'])['payload']
    assert payload['activation'] == 'publication'
    assert result['changes'] == ['subscription bundle', 'publisher reload']
    assert [call[1] for call in env.calls] == ['check', '-e', '-t']
    assert env.pointer['current'] == env.previous and env.store.revision == 0


@pytest.mark.skipif(os.name != 'posix', reason='Real POSIX umask and runtime traversal permissions')
@pytest.mark.parametrize('publish_only', [False, True])
def test_prepare_under_private_manager_umask_keeps_runtime_access(environment, monkeypatch, publish_only):
    env = environment
    config = read_config(env.previous)
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_gid=123)))
    monkeypatch.setattr(deployment.os, 'chown', lambda *a: None)
    monkeypatch.setattr(deployment, 'render_nginx', lambda *a: 'test nginx config')
    monkeypatch.setattr(deployment, 'client_config', lambda *a: {
        'yaml': profile()['yaml'], 'raw': profile()['raw'],
    })
    previous_mask = os.umask(0o077)
    try:
        result = deployment.prepare(env.store, config, publish_only=publish_only)
    finally:
        os.umask(previous_mask)
    directory = env.root / 'revisions' / result['revision']
    assert stat.S_IMODE(directory.stat().st_mode) == 0o711
    assert stat.S_IMODE((directory / 'site').stat().st_mode) == 0o755
    assert stat.S_IMODE((directory / 'sing-box.json').stat().st_mode) == 0o640
    assert stat.S_IMODE((directory / 'site/index.html').stat().st_mode) == 0o644
    assert stat.S_IMODE((directory / 'config.json').stat().st_mode) == 0o600
    deployment._check_runtime_permissions(directory)


@pytest.mark.skipif(os.name != 'posix', reason='Real POSIX runtime permission checks')
@pytest.mark.parametrize('activation', ['full', 'publication'])
@pytest.mark.parametrize('relative,mode', [('', 0o700), ('site', 0o700),
                         ('sing-box.json', 0o600), ('site/index.html', 0o600),
                         ('site', 0o777)])
def test_apply_refuses_unreadable_or_writable_candidate_before_service_changes(environment, activation, relative, mode):
    env = environment
    directory, identity = env.plan(activation=activation)
    (directory / relative).chmod(mode)
    with pytest.raises(OperationError, match='permissions'):
        deployment.apply(env.store, identity)
    assert env.calls == [] and env.pointer['current'] == env.previous
    assert env.store.pending() == [] and env.store.revision == 0


def test_publication_apply_reloads_only_publisher_after_ack(environment, monkeypatch):
    env = environment
    directory, identity = env.plan()
    probes = []
    monkeypatch.setattr(deployment, '_publication_health', lambda path: probes.append(path))
    result = deployment.apply(env.store, identity)
    assert result['activation'] == 'publication' and result['status'] == 'applied'
    assert env.calls == [('publish', directory), ('systemctl', 'reload', 'proxyhub-publisher.service')]
    assert probes == [directory] and env.pointer['current'] == directory
    assert env.store.revision == 1 and env.store.pending() == []
    details = env.store.db.execute("SELECT details FROM journal WHERE stage='activating'").fetchone()[0]
    assert json.loads(details)['activation'] == 'publication'


def test_publication_apply_rechecks_current_server_before_mutation(environment, monkeypatch):
    env = environment
    _, identity = env.plan()
    current = read_config(env.previous)
    current['server']['site_title'] = 'Concurrent server modification'
    (env.previous / 'config.json').write_bytes(canonical(current))
    with pytest.raises(OperationError, match='cannot change server'):
        deployment.apply(env.store, identity)
    assert env.calls == [] and env.store.pending() == []


def test_failed_candidate_ack_restores_old_generation_without_restarts(environment, monkeypatch):
    env = environment
    directory, identity = env.plan()
    probes = []
    def probe(path):
        probes.append(path)
        if path == directory:
            raise OperationError('Publisher generation not loaded')
    monkeypatch.setattr(deployment, '_publication_health', probe)
    with pytest.raises(OperationError, match='generation not loaded'):
        deployment.apply(env.store, identity)
    assert probes == [directory] * 15 + [env.previous]
    assert env.pointer['current'] == env.previous
    assert env.calls == [('publish', directory), ('systemctl', 'reload', 'proxyhub-publisher.service'),
                         ('publish', env.previous), ('systemctl', 'reload', 'proxyhub-publisher.service')]
    assert env.store.inspect(identity)['status'] == 'failed'
    assert env.store.revision == 0 and env.store.pending() == []


def test_failed_rollback_ack_stays_pending_until_recovery_ack(environment, monkeypatch):
    env = environment
    directory, identity = env.plan()
    def unavailable(path):
        raise OperationError('Publisher unavailable')
    monkeypatch.setattr(deployment, '_publication_health', unavailable)
    with pytest.raises(OperationError, match='unavailable'):
        deployment.apply(env.store, identity)
    assert env.store.inspect(identity)['status'] == 'prepared'
    assert len(env.store.pending()) == 1 and env.store.revision == 0
    with pytest.raises(OperationError, match='unavailable'):
        deployment.recover(env.store)
    assert len(env.store.pending()) == 1
    probes = []
    monkeypatch.setattr(deployment, '_publication_health', lambda path: probes.append(path))
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert probes == [env.previous] and env.store.pending() == []
    assert deployment.recover(env.store)['status'] == 'nothing_to_recover'
    assert not any('restart' in call or 'proxyhub-proxy.service' in call
                   or 'proxyhub-nginx.service' in call for call in env.calls)


def test_crash_recovery_uses_publication_mode_and_previous_generation(environment, monkeypatch):
    env = environment
    directory, identity = env.plan()
    env.pointer['current'] = directory
    env.store.record(identity, 'activating', {'previous': str(env.previous),
                     'candidate': str(directory), 'activation': 'publication'})
    probes = []
    monkeypatch.setattr(deployment, '_publication_health', lambda path: probes.append(path))
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == env.previous and probes == [env.previous]
    assert env.calls == [('publish', env.previous), ('systemctl', 'reload', 'proxyhub-publisher.service')]
    assert env.store.inspect(identity)['status'] == 'failed'


def test_crash_recovery_refuses_mismatched_activation_mode(environment):
    env = environment
    directory, identity = env.plan()
    env.store.record(identity, 'activating', {'previous': str(env.previous),
                     'candidate': str(directory), 'activation': 'full'})
    with pytest.raises(OperationError, match='mode differs'):
        deployment.recover(env.store)
    assert len(env.store.pending()) == 1 and env.calls == []


def test_unknown_activation_mode_cannot_change_services(environment):
    env = environment
    _, identity = env.plan(activation='restart-everything')
    with pytest.raises(OperationError, match='Unsupported activation'):
        deployment.apply(env.store, identity)
    assert env.calls == [] and env.store.pending() == []


@pytest.fixture
def live_publisher(tmp_path, monkeypatch):
    directory = tmp_path / ('b' * 32)
    directory.mkdir()
    expected = build_bundle([profile()], directory.name)
    save_bundle(directory / 'bundle.json', expected)
    runtime_bundle = tmp_path / 'runtime-bundle.json'
    save_bundle(runtime_bundle, expected)
    server = PublisherServer(('127.0.0.1', 0), runtime_bundle)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    constructor = deployment.http.client.HTTPConnection
    destinations = []
    def connection(host, port, **kwargs):
        destinations.append((host, port))
        return constructor(host, server.server_port, **kwargs)
    monkeypatch.setattr(deployment.http.client, 'HTTPConnection', connection)
    yield directory, server, runtime_bundle, destinations
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_real_loopback_ack_checks_both_formats_and_fixed_destination(live_publisher):
    directory, _, _, destinations = live_publisher
    deployment._publication_health(directory)
    assert destinations == [('127.0.0.1', 18890)] * 2


@pytest.mark.parametrize('mismatch', ['generation', 'metadata', 'raw_body'])
def test_real_loopback_ack_rejects_stale_or_inconsistent_runtime(live_publisher, mismatch):
    directory, server, bundle_path, _ = live_publisher
    source = profile()
    generation = directory.name
    if mismatch == 'generation':
        generation = 'a' * 32
    elif mismatch == 'metadata':
        source['update_interval_hours'] = 7
    else:
        source['raw'] = base64.b64encode(b'vless://public-other-fixture').decode()
    save_bundle(bundle_path, build_bundle([source], generation))
    assert server.reload_bundle()
    with pytest.raises(OperationError, match='Publisher'):
        deployment._publication_health(directory)
