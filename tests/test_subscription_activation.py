"""Publication transactions keep proxy and Nginx running and require real ACKs."""
from contextlib import nullcontext
import base64
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from proxyhub import deployment
from proxyhub.core import OperationError, canonical, digest
from proxyhub.publication import (advance_security, build_bundle, iter_routes, load_security,
                                  save_bundle, save_security, token_digest)
from proxyhub.proxy import validate_spec
from proxyhub.publisher import PublisherHandler, PublisherServer
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


def publication_modes(root, monkeypatch):
    """Windows only emulates chmod/stat; POSIX permission checks stay real."""
    mode = {'value': 0o750}
    if os.name != 'posix':
        stat_path, chmod_path = Path.stat, Path.chmod
        def directory_stat(path, *args, **kwargs):
            info = stat_path(path, *args, **kwargs)
            return os.stat_result((stat.S_IFDIR | mode['value'], *info[1:])) if path == root/'publish' else info
        def directory_chmod(path, value, *args, **kwargs):
            result = chmod_path(path, value, *args, **kwargs)
            if path == root/'publish':
                mode['value'] = value
            return result
        monkeypatch.setattr(Path, 'stat', directory_stat)
        monkeypatch.setattr(Path, 'chmod', directory_chmod)
    return mode


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    (root / 'revisions').mkdir(parents=True)
    (root / 'publish').mkdir()
    publication_mode = publication_modes(root, monkeypatch)
    (root/'publish').chmod(0o750)
    store = StateStore(tmp_path / 'plans')
    monkeypatch.setattr(store, 'exclusive', nullcontext)
    monkeypatch.setattr(deployment, 'ROOT', root)
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_gid=123)))
    monkeypatch.setattr(deployment.os, 'chown', lambda *a: None, raising=False)
    save_security(root/'publish/security.json', {'schema_version':1,'generation':0,
                                               'revoked_tokens':[],'retired_aliases':[]})
    pointer = {'current': None}
    calls = []
    monkeypatch.setattr(deployment, '_current_revision', lambda: pointer['current'])
    monkeypatch.setattr(deployment, '_link', lambda path: pointer.update(current=path))
    monkeypatch.setattr(deployment, '_publish', lambda path: calls.append(('publish', path)))
    monkeypatch.setattr(deployment, 'run', lambda command, **kwargs: calls.append(tuple(command)))
    monkeypatch.setattr(deployment, 'validate_certificate', lambda *a: {'sha256': 'c' * 64})
    monkeypatch.setattr(deployment, '_certificate_files', lambda server: {'certificate': 'c', 'key': 'k'})
    monkeypatch.setattr(deployment.time, 'sleep', lambda delay: None)

    def revision(char, *, server=None, publication_name='Test subscription', profiles=None):
        directory = root / 'revisions' / (char * 32)
        (directory / 'site').mkdir(parents=True)
        metadata = (profiles or [profile(name=publication_name)])[0]
        config = {'server': server or server_spec(), 'subscription': {
            key: value for key, value in metadata.items() if key not in ('host', 'yaml', 'raw')
        }, 'overrides': {}}
        if profiles is not None and len(profiles)>1:
            config['subscriptions'] = [{**{key:value for key,value in item.items() if key not in ('yaml','raw')},
                                        'node_ids':['local-reality']} for item in profiles[1:]]
        for name in deployment.CANDIDATE_FILES:
            (directory / name).write_bytes(b'test fixture')
        (directory / 'config.json').write_bytes(canonical(config))
        save_bundle(directory / 'bundle.json', build_bundle(profiles or [metadata], directory.name))
        return directory

    previous = revision('a')
    pointer['current'] = previous

    def plan(char='b', *, activation='publication', server=None, profiles=None, subscription_operation=None):
        directory = revision(char, server=server, publication_name='Updated subscription',profiles=profiles)
        payload = {'revision': directory.name, 'activation': activation,
                   'files': {name: digest((directory / name).read_bytes())
                             for name in deployment.CANDIDATE_FILES},
                   'certificate_sha256': 'c' * 64,
                   'certificate_files': {'certificate': 'c', 'key': 'k'}}
        if subscription_operation is not None:
            payload['subscription_operation'] = subscription_operation
        identity = store.plan('deployment', payload)
        return directory, identity

    yield SimpleNamespace(root=root, store=store, pointer=pointer, calls=calls, publication_mode=publication_mode,
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
    monkeypatch.setattr(deployment,'ROOT',tmp_path)
    (tmp_path/'publish').mkdir()
    security = tmp_path/'publish/security.json'
    save_security(security,{'schema_version':1,'generation':0,'revoked_tokens':[],'retired_aliases':[]})
    directory = tmp_path / ('b' * 32)
    directory.mkdir()
    expected = build_bundle([profile()], directory.name)
    save_bundle(directory / 'bundle.json', expected)
    runtime_bundle = tmp_path / 'runtime-bundle.json'
    save_bundle(runtime_bundle, expected)
    server = PublisherServer(('127.0.0.1', 0), runtime_bundle,security)
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
    assert destinations == [('127.0.0.1', 18890)] * 5


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


@pytest.fixture
def publication_runtime(environment, monkeypatch):
    """Use the real snapshot, independent ledger and HTTP ACK for transactions."""
    env = environment
    runtime_bundle = env.root/'publish/bundle.json'
    save_bundle(runtime_bundle,json.loads((env.previous/'bundle.json').read_text(encoding='utf-8')))
    server = PublisherServer(('127.0.0.1',0),runtime_bundle,env.root/'publish/security.json')
    thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    thread.start()
    closed = False
    constructor = deployment.http.client.HTTPConnection
    requests = []
    def connection(host,port,**kwargs):
        assert (host,port)==('127.0.0.1',18890)
        return constructor(host,server.server_port,**kwargs)
    def publish(directory):
        env.calls.append(('publish',directory))
        save_bundle(runtime_bundle,json.loads((directory/'bundle.json').read_text(encoding='utf-8')))
    def run(command,**kwargs):
        nonlocal server, thread, closed
        env.calls.append(tuple(command))
        if command==['systemctl','stop','proxyhub-publisher.service'] and not closed:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            closed = True
        elif command==['systemctl','start','proxyhub-publisher.service'] and closed:
            server = PublisherServer(('127.0.0.1',0),runtime_bundle,env.root/'publish/security.json')
            thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
            thread.start()
            runtime.server = server
            closed = False
        elif command==['systemctl','reload','proxyhub-publisher.service']:
            assert not closed
            assert server.reload_bundle()
    monkeypatch.setattr(deployment.http.client,'HTTPConnection',connection)
    monkeypatch.setattr(deployment,'_publish',publish)
    monkeypatch.setattr(deployment,'run',run)
    original_header = PublisherHandler.send_header
    failure = {'method':None,'path':None}
    def header(handler,key,value):
        if key=='Profile-Update-Interval':
            requests.append((handler.command,handler.path))
            if handler.command==failure['method'] and handler.path==failure['path']:
                value='168' if value!='168' else '167'
        return original_header(handler,key,value)
    monkeypatch.setattr(PublisherHandler,'send_header',header)
    def request(host,path,method='GET'):
        client=constructor('127.0.0.1',server.server_port,timeout=2)
        try:
            client.request(method,path,headers={'Host':host})
            response=client.getresponse()
            return response.status,dict(response.getheaders()),response.read()
        finally:
            client.close()
    runtime = SimpleNamespace(env=env,server=server,failure=failure,requests=requests,
                              publish=publish,request=request,runtime_bundle=runtime_bundle)
    yield runtime
    if not closed:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def multi_profiles():
    return [profile(),profile(id='second',token='S'*43,name='Second',path_prefix='/second',
                              formats=['yaml','raw'],aliases=[{'id':'alternate','host':'sub.example.test',
                                                              'path_prefix':'/second-alias'}])]


def replace_profiles(directory,profiles):
    config=read_config(directory)
    config['subscription']={key:value for key,value in profiles[0].items() if key not in ('host','yaml','raw')}
    config['subscriptions']=[{**{key:value for key,value in item.items() if key not in ('yaml','raw')},
                              'node_ids':['local-reality']} for item in profiles[1:]]
    (directory/'config.json').write_bytes(canonical(config))
    save_bundle(directory/'bundle.json',build_bundle(profiles,directory.name))


@pytest.mark.parametrize('method',['GET','HEAD'])
def test_last_profile_alias_ack_mismatch_cannot_commit(publication_runtime,method):
    runtime=publication_runtime
    env=runtime.env
    directory,identity=env.plan(profiles=multi_profiles())
    last=next(route for route in iter_routes(json.loads((directory/'bundle.json').read_text())['profiles'][-1])
              if route['path_prefix']=='/second-alias' and route['format']=='yaml')
    runtime.failure.update(method=method,path=last['path'])
    with pytest.raises(OperationError,match='metadata differs'):
        deployment.apply(env.store,identity)
    assert (method,last['path']) in runtime.requests
    assert env.store.inspect(identity)['status']=='failed'
    assert env.store.revision==0 and not env.store.pending() and env.pointer['current']==env.previous
    assert not any('restart' in call for call in env.calls)


def test_all_profiles_must_recover_before_pending_clears(publication_runtime):
    runtime=publication_runtime
    env=runtime.env
    profiles=multi_profiles()
    replace_profiles(env.previous,profiles)
    runtime.publish(env.previous)
    assert runtime.server.reload_bundle()
    directory,identity=env.plan(profiles=profiles)
    last=list(iter_routes(json.loads((directory/'bundle.json').read_text())['profiles'][-1]))[-1]
    runtime.failure.update(method='HEAD',path=last['path'])
    with pytest.raises(OperationError,match='metadata differs'):
        deployment.apply(env.store,identity)
    assert env.store.inspect(identity)['status']=='prepared' and len(env.store.pending())==1
    with pytest.raises(OperationError,match='metadata differs'):
        deployment.recover(env.store)
    assert len(env.store.pending())==1 and env.store.revision==0
    runtime.failure.update(method=None,path=None)
    assert deployment.recover(env.store)['status']=='recovered'
    assert not env.store.pending() and env.pointer['current']==env.previous
    before=len(env.calls)
    assert deployment.recover(env.store)['status']=='nothing_to_recover'
    assert len(env.calls)==before


def test_all_disabled_bundle_requires_real_status_ack(live_publisher):
    directory,server,path,destinations=live_publisher
    disabled=build_bundle([profile(enabled=False)],directory.name)
    save_bundle(directory/'bundle.json',disabled)
    save_bundle(path,disabled)
    assert server.reload_bundle()
    deployment._publication_health(directory)
    assert destinations==[('127.0.0.1',18890)]
    stale=build_bundle([], 'a'*32)
    save_bundle(path,stale)
    assert server.reload_bundle()
    with pytest.raises(OperationError,match='expected generation'):
        deployment._publication_health(directory)


@pytest.mark.parametrize('failure',['publication','interruption'])
def test_rotation_failure_keeps_all_old_aliases_revoked_and_recovery_is_idempotent(publication_runtime,monkeypatch,failure):
    runtime=publication_runtime
    env=runtime.env
    old=profile(aliases=[{'id':'alternate','host':'sub.example.test','path_prefix':'/alternate'}])
    replace_profiles(env.previous,[old])
    runtime.publish(env.previous)
    assert runtime.server.reload_bundle()
    updated={**old,'token':'U'*43}
    directory,identity=env.plan(profiles=[updated],subscription_operation={
        'kind':'subscription.token.rotate','subscription_id':'default'})
    class Interrupted(BaseException):
        pass
    def fail_publish(path):
        if path==directory:
            raise OperationError('publication failed') if failure=='publication' else Interrupted()
        runtime.publish(path)
    monkeypatch.setattr(deployment,'_publish',fail_publish)
    with pytest.raises(OperationError if failure=='publication' else Interrupted):
        deployment.apply(env.store,identity)
    security=load_security(env.root/'publish/security.json')
    assert security['revoked_tokens']==[token_digest(old['token'])]
    assert token_digest(updated['token']) not in security['revoked_tokens']
    for route in iter_routes(build_bundle([old],'old')['profiles'][0]):
        for method in ('GET','HEAD'):
            status,headers,body=runtime.request(route['host'],route['path'],method)
            assert status==404 and body==b'' and headers['X-ProxyHub-Security-Generation']=='1'
    if failure=='interruption':
        assert len(env.store.pending())==1
        assert deployment.recover(env.store)['status']=='recovered'
    assert not env.store.pending() and env.pointer['current']==env.previous
    assert load_security(env.root/'publish/security.json')==security
    before=len(env.calls)
    assert deployment.recover(env.store)['status']=='nothing_to_recover'
    assert len(env.calls)==before


@pytest.mark.parametrize('damage',['deleted','generation-rollback'])
def test_security_floor_prevents_reinitializing_or_rolling_back_ledger(environment,damage):
    env=environment
    path=env.root/'publish/security.json'
    initial=deployment._ensure_publication_security(env.store)
    advance_security(path,tokens=['R'*43])
    assert deployment._ensure_publication_security(env.store)['generation']==1
    if damage=='deleted':
        path.unlink()
    else:
        path.write_bytes(canonical(initial))
    with pytest.raises(OperationError,match='unavailable|backwards'):
        deployment._ensure_publication_security(env.store)
    assert env.store.db.execute("SELECT value FROM metadata WHERE key='publication_security_generation'").fetchone()[0]=='1'
    if damage=='deleted':
        assert not path.exists()


def test_restoring_revoked_old_configuration_does_not_revoke_newer_token(publication_runtime):
    runtime=publication_runtime
    env=runtime.env
    old=profile(aliases=[{'id':'alternate','host':'sub.example.test','path_prefix':'/alternate'}])
    replace_profiles(env.previous,[old])
    runtime.publish(env.previous)
    assert runtime.server.reload_bundle()
    updated={**old,'token':'U'*43}
    directory,identity=env.plan(profiles=[updated],subscription_operation={
        'kind':'subscription.token.rotate','subscription_id':'default'})
    assert deployment.apply(env.store,identity)['status']=='applied'
    security=load_security(env.root/'publish/security.json')
    assert security['revoked_tokens']==[token_digest(old['token'])]
    restored,restore_id=env.plan('c',profiles=[old])
    assert deployment.apply(env.store,restore_id)['status']=='applied'
    assert load_security(env.root/'publish/security.json')==security
    for route in iter_routes(build_bundle([old],'old')['profiles'][0]):
        assert runtime.request(route['host'],route['path'])[0]==404
    assert env.pointer['current']==restored and not env.store.pending()


def test_direct_typed_alias_retirement_cannot_bypass_shared_route_protection(environment, monkeypatch):
    env = environment
    monkeypatch.setattr(deployment, '_wait_publication', lambda path: None)
    original = profile(aliases=[{'id': 'shared', 'host': 'sub.example.test', 'path_prefix': '/shared'}])
    other = profile(id='second', token='S' * 43, name='Second', path_prefix='/shared')
    replace_profiles(env.previous, [original, other])
    updated = {**original, 'aliases': []}
    directory, identity = env.plan(profiles=[updated, other], subscription_operation={
        'kind': 'subscription.alias.retire', 'subscription_id': 'default'})
    with pytest.raises(OperationError, match='referenced|another subscription'):
        deployment.apply(env.store, identity)
    assert env.pointer['current'] == env.previous and not env.store.pending()
    assert env.calls == [] and load_security(env.root/'publish/security.json')['generation'] == 0


def test_typed_alias_add_cannot_introduce_a_permanently_retired_route(environment, monkeypatch):
    env = environment
    monkeypatch.setattr(deployment, '_wait_publication', lambda path: None)
    alias = {'id': 'retired', 'host': 'sub.example.test', 'path_prefix': '/retired'}
    advance_security(env.root/'publish/security.json', aliases=[alias])
    directory, identity = env.plan(profiles=[profile(aliases=[alias])], subscription_operation={
        'kind': 'subscription.alias.add', 'subscription_id': 'default'})
    with pytest.raises(OperationError, match='retired|revoked|closed'):
        deployment.apply(env.store, identity)
    assert env.pointer['current'] == env.previous and not env.store.pending() and env.calls == []


def test_injected_security_write_failure_stops_publication_until_recovery_finishes_revocation(publication_runtime, monkeypatch):
    """The ledger I/O fault and systemctl boundary are injected, not VPS faults."""
    runtime = publication_runtime
    env = runtime.env
    original = profile(aliases=[{'id': 'alternate', 'host': 'sub.example.test', 'path_prefix': '/alternate'}])
    other = profile(id='second', token='S' * 43, name='Second', path_prefix='/second')
    replace_profiles(env.previous, [original, other])
    runtime.publish(env.previous)
    assert runtime.server.reload_bundle()
    updated = {**original, 'token': 'U' * 43}
    directory, identity = env.plan(profiles=[updated, other], subscription_operation={
        'kind': 'subscription.token.rotate', 'subscription_id': 'default'})
    env.calls.clear()
    fault = {'write': True, 'health': False}
    advance = deployment.advance_security
    original_run = deployment.run
    health = deployment._publication_health
    events = []

    def injected_write(*args, **kwargs):
        if fault['write']:
            raise OSError('injected atomic security write failure')
        result = advance(*args, **kwargs)
        events.append('revocation-durable')
        return result

    def service(command, **kwargs):
        if command == ['systemctl', 'start', 'proxyhub-publisher.service']:
            assert token_digest(original['token']) in load_security(env.root/'publish/security.json')['revoked_tokens']
            events.append('publisher-start')
        return original_run(command, **kwargs)

    def checked_health(path, **kwargs):
        health(path, **kwargs)
        if fault['health'] and not kwargs.get('readiness_only'):
            raise OperationError('injected all-profile acknowledgement failure')

    monkeypatch.setattr(deployment, 'advance_security', injected_write)
    monkeypatch.setattr(deployment, 'run', service)
    monkeypatch.setattr(deployment, '_publication_health', checked_health)
    with pytest.raises((OSError, OperationError)):
        deployment.apply(env.store, identity)
    assert env.store.inspect(identity)['status'] == 'prepared' and len(env.store.pending()) == 1
    assert env.pointer['current'] == env.previous and env.store.revision == 0
    assert ('systemctl', 'stop', 'proxyhub-publisher.service') in env.calls
    assert not any(call[0] == 'publish' for call in env.calls)
    with pytest.raises((OSError, OperationError)):
        deployment.recover(env.store)
    assert len(env.store.pending()) == 1 and 'publisher-start' not in events
    fault.update(write=False, health=True)
    with pytest.raises(OperationError, match='all-profile acknowledgement failure'):
        deployment.recover(env.store)
    assert len(env.store.pending()) == 1 and env.store.revision == 0
    assert events.index('revocation-durable') < events.index('publisher-start')
    fault['health'] = False
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == env.previous and not env.store.pending()
    for route in iter_routes(build_bundle([original], 'old')['profiles'][0]):
        for method in ('GET', 'HEAD'):
            assert runtime.request(route['host'], route['path'], method)[0] == 404
    other_route = next(iter_routes(build_bundle([other], 'other')['profiles'][0]))
    assert runtime.request(other_route['host'], other_route['path'])[0] == 200
    assert token_digest(updated['token']) not in load_security(env.root/'publish/security.json')['revoked_tokens']
    before = len(env.calls)
    assert deployment.recover(env.store)['status'] == 'nothing_to_recover' and len(env.calls) == before


@pytest.mark.parametrize('damaged_revision', ['previous', 'candidate'])
def test_recovery_checks_previous_manifest_before_retrying_publication_safety(environment, monkeypatch, damaged_revision):
    env = environment
    directory, identity = env.plan(profiles=[profile(token='U' * 43)], subscription_operation={
        'kind': 'subscription.token.rotate', 'subscription_id': 'default'})
    save_bundle(env.root/'publish/bundle.json', json.loads((env.previous/'bundle.json').read_text()))
    marker = env.root/'intent-before-damaged-revision.json'
    child = subprocess.run([sys.executable, '-c',
        "import runpy,sys; runpy.run_path(sys.argv[1])['_exit_after_safety_failure_stop'](*sys.argv[2:])",
        str(Path(__file__).resolve()), str(env.root), str(env.store.root), str(env.previous), identity,
        str(marker), 'intent-recorded'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
    assert child.returncode == 86, child.stdout
    assert json.loads(marker.read_text()) == {'publisher_stopped_before_exit': False, 'publish_mode': 0o750}
    target = env.previous if damaged_revision == 'previous' else directory
    damaged = read_config(target)
    damaged['subscription']['token'] = 'V' * 43
    (target/'config.json').write_bytes(canonical(damaged))
    effects = []
    monkeypatch.setattr(deployment, '_revoke_publication_changes', lambda *args: effects.append('revoke'))
    monkeypatch.setattr(deployment, '_wait_publication', lambda *args: effects.append('acknowledge'))
    reopened = StateStore(env.store.root)
    reopened.exclusive = nullcontext
    try:
        assert stat.S_IMODE((env.root/'publish').stat().st_mode) == 0o750
        with pytest.raises(OperationError, match='changed after validation'):
            deployment.check_publication_start(reopened)
        with pytest.raises(OperationError, match='changed after validation'):
            deployment.recover(reopened)
        assert stat.S_IMODE((env.root/'publish').stat().st_mode) == 0o700
        assert effects == [] and env.calls == []
        assert load_security(env.root/'publish/security.json')['generation'] == 0
        assert len(reopened.pending()) == 1 and reopened.inspect(identity)['status'] == 'prepared'
    finally:
        reopened.db.close()


def _exit_after_safety_failure_stop(root, plans, previous, identity, marker, exit_point):
    """Run in an isolated process: no exception handling or SQLite close at exit."""
    root, previous = Path(root), Path(previous)
    deployment.ROOT = root
    deployment._current_revision = lambda: previous
    deployment.validate_certificate = lambda *a: {'sha256': 'c' * 64}
    deployment._certificate_files = lambda *a: {'certificate': 'c', 'key': 'k'}
    deployment._check_runtime_permissions = lambda *a: None
    deployment.os.chown = lambda *a: None
    sys.modules['pwd'] = SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_gid=123))
    store = StateStore(Path(plans))
    store.exclusive = nullcontext
    publication_modes(root, pytest.MonkeyPatch())
    server = PublisherServer(('127.0.0.1', 0), root/'publish/bundle.json', root/'publish/security.json')
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    record = store.record
    gate = deployment._publication_gate

    def exit_marker():
        Path(marker).write_text(json.dumps({'publisher_stopped_before_exit': False,
            'publish_mode': stat.S_IMODE((root/'publish').stat().st_mode)}), encoding='utf-8')
        os._exit(86)

    def activating(*args):
        if args[1] == 'activating' and exit_point == 'before-intent':
            exit_marker()
        record(*args)
        if args[1] == 'activating' and exit_point == 'intent-recorded':
            exit_marker()

    def close_gate(*, closed):
        gate(closed=closed)
        if closed and exit_point == 'gate-closed':
            exit_marker()

    store.record = activating
    deployment._publication_gate = close_gate

    def fail_write(*args, **kwargs):
        raise OSError('injected atomic security write failure')

    def stop(command, **kwargs):
        assert command == ['systemctl', 'stop', 'proxyhub-publisher.service']
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        try:
            with socket.create_connection(('127.0.0.1', server.server_port), timeout=.2):
                raise AssertionError('Publisher listener remained open after stop')
        except OSError:
            pass
        Path(marker).write_text(json.dumps({'publisher_stopped_before_exit': True,
            'publish_mode': stat.S_IMODE((root/'publish').stat().st_mode)}), encoding='utf-8')
        os._exit(86)

    deployment.advance_security = fail_write
    deployment.run = stop
    deployment.apply(store, identity)
    raise AssertionError('Expected the actual process exit at the completed-stop boundary')


@pytest.mark.parametrize('kind', ['subscription.token.rotate', 'subscription.alias.retire'])
@pytest.mark.parametrize('exit_point', ['stop-complete', 'intent-recorded'])
def test_safety_failure_stop_crash_keeps_durable_recovery_intent(environment, monkeypatch, kind, exit_point):
    env = environment
    original = profile(aliases=[{'id': 'alternate', 'host': 'sub.example.test', 'path_prefix': '/alternate'}])
    other = profile(id='second', token='S' * 43, name='Second', path_prefix='/second')
    replace_profiles(env.previous, [original, other])
    save_bundle(env.root/'publish/bundle.json', json.loads((env.previous/'bundle.json').read_text()))
    updated = {**original, **({'token': 'U' * 43} if kind.endswith('rotate') else {'aliases': []})}
    directory, identity = env.plan(profiles=[updated, other], subscription_operation={
        'kind': kind, 'subscription_id': 'default'})
    marker = env.root/'completed-stop.json'
    result = subprocess.run([sys.executable, '-c',
        "import runpy,sys; runpy.run_path(sys.argv[1])['_exit_after_safety_failure_stop'](*sys.argv[2:])",
        str(Path(__file__).resolve()), str(env.root), str(env.store.root), str(env.previous), identity, str(marker), exit_point],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
    assert result.returncode == 86, result.stdout
    expected_mode = 0o750 if exit_point == 'intent-recorded' else 0o700
    assert json.loads(marker.read_text()) == {'publisher_stopped_before_exit': exit_point == 'stop-complete',
                                              'publish_mode': expected_mode}
    if os.name != 'posix':
        env.publication_mode['value'] = expected_mode
    reopened = StateStore(env.store.root)
    reopened.exclusive = nullcontext
    server = thread = None
    commands, requests, events = [], [], []
    fault = {'write': True, 'ack': False}
    constructor = deployment.http.client.HTTPConnection
    advance = deployment.advance_security
    health = deployment._publication_health
    handle = PublisherHandler.do_GET

    def close_publisher():
        nonlocal server, thread
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            server = thread = None

    def start_publisher():
        nonlocal server, thread
        server = PublisherServer(('127.0.0.1', 0), env.root/'publish/bundle.json', env.root/'publish/security.json')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()

    def service(command, **kwargs):
        commands.append(tuple(command))
        if command == ['systemctl', 'stop', 'proxyhub-publisher.service']:
            close_publisher()
            events.append('publisher-stopped')
        elif command == ['systemctl', 'start', 'proxyhub-publisher.service']:
            assert deployment.check_publication_start(reopened) == {'status': 'publication_safe'}
            if server is None:
                start_publisher()
            events.append('publisher-started')
        elif command == ['systemctl', 'reload', 'proxyhub-publisher.service']:
            assert server.reload_bundle()

    def write_security(*args, **kwargs):
        events.append('revocation-attempt')
        if fault['write']:
            events.append('revocation-failed')
            raise OSError('injected persistent atomic security write failure')
        result = advance(*args, **kwargs)
        events.append('revocation-durable')
        return result

    def connection(host, port, **kwargs):
        assert (host, port) == ('127.0.0.1', 18890)
        return constructor(host, server.server_port, **kwargs)

    def request(handler):
        requests.append((handler.command, handler.path))
        return handle(handler)

    def checked_health(path, **kwargs):
        health(path, **kwargs)
        if fault['ack'] and not kwargs.get('readiness_only'):
            raise OperationError('injected all-profile acknowledgement failure')

    monkeypatch.setattr(deployment, 'run', service)
    monkeypatch.setattr(deployment, 'advance_security', write_security)
    monkeypatch.setattr(deployment.http.client, 'HTTPConnection', connection)
    monkeypatch.setattr(deployment, '_publication_health', checked_health)
    monkeypatch.setattr(PublisherHandler, 'do_GET', request)
    monkeypatch.setattr(PublisherHandler, 'do_HEAD', request)
    try:
        pending = reopened.pending()
        assert len(pending) == 1, 'Completed stop lost the unfinished revocation intent after actual os._exit(86)'
        assert pending[0]['plan_id'] == identity
        assert reopened.inspect(identity)['status'] == 'prepared'
        assert pending[0]['details']['previous'] == str(env.previous)
        assert pending[0]['details']['candidate'] == str(directory)
        assert pending[0]['details']['publication_safety_failed'] is True
        with pytest.raises(OperationError):
            deployment.check_publication_start(reopened)
        assert commands == [] and requests == []
        assert load_security(env.root/'publish/security.json')['generation'] == 0
        if exit_point == 'intent-recorded':
            # Model the independent service which outlives an interrupted CLI.
            start_publisher()
        with pytest.raises((OSError, OperationError)):
            deployment.recover(reopened)
        assert len(reopened.pending()) == 1 and server is None
        assert 'publisher-stopped' in events and 'publisher-started' not in events
        assert events.index('revocation-attempt') < events.index('revocation-failed') < events.index('publisher-stopped')
        with pytest.raises(OperationError):
            deployment.check_publication_start(reopened)
        fault.update(write=False, ack=True)
        with pytest.raises(OperationError, match='all-profile acknowledgement failure'):
            deployment.recover(reopened)
        assert len(reopened.pending()) == 1 and reopened.revision == 0
        assert events.index('revocation-durable') < events.index('publisher-started')
        fault['ack'] = False
        assert deployment.recover(reopened)['status'] == 'recovered'
        assert not reopened.pending() and env.pointer['current'] == env.previous
        assert deployment.check_publication_start(reopened) == {'status': 'publication_safe'}
        for member in build_bundle([original, other], 'old')['profiles']:
            for route in iter_routes(member):
                for method in ('GET', 'HEAD'):
                    assert (method, route['path']) in requests
                    client = constructor('127.0.0.1', server.server_port, timeout=2)
                    try:
                        client.request(method, route['path'], headers={'Host': route['host']})
                        response = client.getresponse()
                        allowed = member['id'] == 'second' or (kind.endswith('retire') and route['path_prefix'] == '/sub')
                        assert response.status == (200 if allowed else 404)
                        assert response.read() == (route['body'].encode() if allowed and method == 'GET' else b'')
                    finally:
                        client.close()
        assert token_digest(updated['token']) not in load_security(env.root/'publish/security.json')['revoked_tokens']
        before = (len(commands), len(requests))
        assert deployment.recover(reopened)['status'] == 'nothing_to_recover'
        assert before == (len(commands), len(requests))
    finally:
        close_publisher()
        reopened.db.close()


def test_safety_recovery_accepts_candidate_loaded_before_rollback(publication_runtime, monkeypatch):
    runtime = publication_runtime
    env = runtime.env
    original, other = multi_profiles()
    replace_profiles(env.previous, [original, other])
    runtime.publish(env.previous)
    assert runtime.server.reload_bundle()
    updated = {**original, 'token': 'U' * 43}
    directory, identity = env.plan(profiles=[updated, other], subscription_operation={
        'kind': 'subscription.token.rotate', 'subscription_id': 'default'})

    class Interrupted(BaseException):
        pass

    def interrupted_commit(plan_id):
        assert plan_id == identity
        assert runtime.server.snapshot.generation == directory.name
        raise Interrupted()

    monkeypatch.setattr(env.store, 'commit', interrupted_commit)
    with pytest.raises(Interrupted):
        deployment.apply(env.store, identity)
    assert len(env.store.pending()) == 1
    candidate_route = next(iter_routes(build_bundle([updated], 'candidate')['profiles'][0]))
    assert runtime.request(candidate_route['host'], candidate_route['path'])[0] == 200
    runtime.requests.clear()
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == env.previous and not env.store.pending()
    for member in build_bundle([original, other], 'previous')['profiles']:
        for route in iter_routes(member):
            for method in ('GET', 'HEAD'):
                assert (method, route['path']) in runtime.requests or member['id'] == 'default'
                assert runtime.request(route['host'], route['path'], method)[0] == (404 if member['id'] == 'default' else 200)
    assert token_digest(updated['token']) not in load_security(env.root/'publish/security.json')['revoked_tokens']
    before = (len(env.calls), len(runtime.requests))
    assert deployment.recover(env.store)['status'] == 'nothing_to_recover'
    assert before == (len(env.calls), len(runtime.requests))


@pytest.mark.parametrize('kind', ['subscription.token.rotate', 'subscription.alias.retire'])
@pytest.mark.parametrize('exit_point', ['intent-recorded', 'gate-closed'])
def test_unrelated_safety_plan_cannot_reopen_another_plans_closed_gate(publication_runtime, monkeypatch,
                                                                    kind, exit_point):
    """The original durable intent owns denial before and after gate closure."""
    runtime = publication_runtime
    env = runtime.env
    original, other = multi_profiles()
    original = {**original, 'aliases': [{'id': 'original-alias', 'host': 'sub.example.test',
                                      'path_prefix': '/original-alias'}]}
    replace_profiles(env.previous, [original, other])
    runtime.publish(env.previous)
    assert runtime.server.reload_bundle()
    denied_route = next(route for route in iter_routes(build_bundle([original], 'old')['profiles'][0])
                        if kind.endswith('rotate') or route['path_prefix'] == '/original-alias')
    assert runtime.request(denied_route['host'], denied_route['path'])[0] == 200
    updated = {**original, **({'token': 'U' * 43} if kind.endswith('rotate') else {'aliases': []})}
    directory, original_identity = env.plan(profiles=[updated, other], subscription_operation={
        'kind': kind, 'subscription_id': 'default'})
    _, other_identity = env.plan('c', profiles=[original, {**other, 'token': 'V' * 43}],
                                subscription_operation={'kind': 'subscription.token.rotate',
                                                        'subscription_id': 'second'})
    _, metadata_identity = env.plan('d', profiles=[{**original, 'name': 'Metadata only'}, other])
    marker = env.root/'cross-plan-exit.json'
    child = subprocess.run([sys.executable, '-c',
        "import runpy,sys; runpy.run_path(sys.argv[1])['_exit_after_safety_failure_stop'](*sys.argv[2:])",
        str(Path(__file__).resolve()), str(env.root), str(env.store.root), str(env.previous),
        original_identity, str(marker), exit_point], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=20)
    assert child.returncode == 86, child.stdout
    expected_mode = 0o750 if exit_point == 'intent-recorded' else 0o700
    assert json.loads(marker.read_text()) == {'publisher_stopped_before_exit': False,
                                              'publish_mode': expected_mode}
    if os.name != 'posix':
        env.publication_mode['value'] = expected_mode
    reopened = StateStore(env.store.root)
    reopened.exclusive = nullcontext
    requests = []
    handle = PublisherHandler.do_GET
    def request(handler):
        requests.append((handler.command, handler.path))
        return handle(handler)
    monkeypatch.setattr(PublisherHandler, 'do_GET', request)
    monkeypatch.setattr(PublisherHandler, 'do_HEAD', request)
    try:
        pending = reopened.pending()
        assert len(pending) == 1 and pending[0]['plan_id'] == original_identity
        assert pending[0]['details']['previous'] == str(env.previous)
        assert pending[0]['details']['candidate'] == str(directory)
        # A durable intent is not itself an HTTP gate: the live publisher may
        # still serve until chmod. Real low-uid gate denial has its POSIX test.
        old_http_before_gate = runtime.request(denied_route['host'], denied_route['path'])[0] if expected_mode == 0o750 else None
        assert old_http_before_gate in (None, 200)
        before = (len(env.calls), len(requests))
        for identity in (other_identity, metadata_identity):
            with pytest.raises(OperationError):
                deployment.apply(reopened, identity)
        with pytest.raises(OperationError):
            deployment.check_publication_start(reopened)
        assert before == (len(env.calls), len(requests))
        assert reopened.revision == 0 and reopened.pending() == pending
        assert stat.S_IMODE((env.root/'publish').stat().st_mode) == expected_mode
        security = load_security(env.root/'publish/security.json')
        assert security['revoked_tokens'] == [] and security['retired_aliases'] == []
        assert deployment.recover(reopened)['status'] == 'recovered'
        assert not reopened.pending() and env.pointer['current'] == env.previous
        assert stat.S_IMODE((env.root/'publish').stat().st_mode) == 0o750
        assert deployment.check_publication_start(reopened) == {'status': 'publication_safe'}
        security = load_security(env.root/'publish/security.json')
        assert token_digest(updated['token']) not in security['revoked_tokens']
        assert token_digest(other['token']) not in security['revoked_tokens']
        assert reopened.inspect(other_identity)['status'] == 'prepared'
        for member in build_bundle([original, other], 'old')['profiles']:
            for route in iter_routes(member):
                for method in ('GET', 'HEAD'):
                    assert (method, route['path']) in requests
                    allowed = member['id'] == 'second' or (kind.endswith('retire')
                                                           and route['path_prefix'] != '/original-alias')
                    status, headers, body = runtime.request(route['host'], route['path'], method)
                    assert status == (200 if allowed else 404)
                    assert body == (route['body'].encode() if allowed and method == 'GET' else b'')
                    if allowed:
                        assert headers['X-ProxyHub-Generation'] == env.previous.name
                        assert headers['X-ProxyHub-Security-Generation'] == str(security['generation'])
                        for key, value in route['headers'].items():
                            assert headers[key] == value
        before = (len(env.calls), len(requests))
        assert deployment.recover(reopened)['status'] == 'nothing_to_recover'
        assert before == (len(env.calls), len(requests))
        print(json.dumps({'exit_point': exit_point, 'child_exit_code': child.returncode,
                          'original_intent_preserved': True, 'other_plans_rejected': True,
                          'old_http_before_gate': old_http_before_gate, 'recovery': 'recovered',
                          'all_profile_routes_confirmed': True}))
    finally:
        reopened.db.close()


def test_crash_before_durable_intent_has_not_closed_gate(publication_runtime):
    runtime = publication_runtime
    env = runtime.env
    original, other = multi_profiles()
    replace_profiles(env.previous, [original, other])
    runtime.publish(env.previous)
    assert runtime.server.reload_bundle()
    _, identity = env.plan(profiles=[{**original, 'token': 'U' * 43}, other], subscription_operation={
        'kind': 'subscription.token.rotate', 'subscription_id': 'default'})
    marker = env.root/'before-intent.json'
    result = subprocess.run([sys.executable, '-c',
        "import runpy,sys; runpy.run_path(sys.argv[1])['_exit_after_safety_failure_stop'](*sys.argv[2:])",
        str(Path(__file__).resolve()), str(env.root), str(env.store.root), str(env.previous), identity,
        str(marker), 'before-intent'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
    assert result.returncode == 86, result.stdout
    assert json.loads(marker.read_text()) == {'publisher_stopped_before_exit': False, 'publish_mode': 0o750}
    reopened = StateStore(env.store.root)
    reopened.exclusive = nullcontext
    try:
        assert reopened.pending() == [] and reopened.inspect(identity)['status'] == 'prepared'
        assert stat.S_IMODE((env.root/'publish').stat().st_mode) == 0o750
        before = (len(env.calls), len(runtime.requests))
        assert deployment.check_publication_start(reopened) == {'status': 'publication_safe'}
        assert deployment.recover(reopened)['status'] == 'nothing_to_recover'
        assert before == (len(env.calls), len(runtime.requests))
        assert load_security(env.root/'publish/security.json')['revoked_tokens'] == []
        for member in build_bundle([original, other], 'old')['profiles']:
            for route in iter_routes(member):
                assert runtime.request(route['host'], route['path'])[0] == 200
    finally:
        reopened.db.close()


def test_orphan_closed_gate_requires_investigation_and_cannot_be_unmasked(publication_runtime):
    runtime = publication_runtime
    env = runtime.env
    original, other = multi_profiles()
    original = {**original, 'aliases': [{'id': 'original-alias', 'host': 'sub.example.test',
                                      'path_prefix': '/original-alias'}]}
    replace_profiles(env.previous, [original, other])
    runtime.publish(env.previous)
    assert runtime.server.reload_bundle()
    _, rotate = env.plan(profiles=[{**original, 'token': 'U' * 43}, other], subscription_operation={
        'kind': 'subscription.token.rotate', 'subscription_id': 'default'})
    _, retire = env.plan('c', profiles=[{**original, 'aliases': []}, other], subscription_operation={
        'kind': 'subscription.alias.retire', 'subscription_id': 'default'})
    _, metadata = env.plan('d', profiles=[{**original, 'name': 'Metadata only'}, other])
    deployment._publication_gate(closed=True)
    before = (len(env.calls), len(runtime.requests))
    for identity in (rotate, retire, metadata):
        with pytest.raises(OperationError):
            deployment.apply(env.store, identity)
        assert env.store.inspect(identity)['status'] == 'prepared'
    with pytest.raises(OperationError):
        deployment.check_publication_start(env.store)
    with pytest.raises(OperationError):
        deployment.recover(env.store)
    assert env.store.pending() == [] and env.store.revision == 0
    assert stat.S_IMODE((env.root/'publish').stat().st_mode) == 0o700
    assert load_security(env.root/'publish/security.json')['generation'] == 0
    assert before == (len(env.calls), len(runtime.requests))


def _run_unprivileged_publisher(directory_fd, uid, gid, port):
    os.setgroups([])
    os.setgid(int(gid))
    os.setuid(int(uid))
    directory = Path('/proc/self/fd')/str(directory_fd)
    try:
        server = PublisherServer(('127.0.0.1', int(port)), directory/'bundle.json', directory/'security.json')
    except PermissionError:
        os._exit(77)
    print(json.dumps({'uid': os.geteuid(), 'gid': os.getegid()}), flush=True)
    server.serve_forever(poll_interval=.01)


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0,
                    reason='Requires root and real POSIX uid/directory permissions')
def test_read_gate_blocks_real_unprivileged_requests_and_prechecked_startup(tmp_path, monkeypatch):
    import pwd
    import time
    account = pwd.getpwnam('nobody')
    root = tmp_path/'runtime'
    publish = root/'publish'
    publish.mkdir(parents=True, mode=0o750)
    os.chown(publish, 0, account.pw_gid)
    publish.chmod(0o750)
    profiles = multi_profiles()
    save_bundle(publish/'bundle.json', build_bundle(profiles, 'previous'))
    save_security(publish/'security.json', {'schema_version': 1, 'generation': 0,
                                          'revoked_tokens': [], 'retired_aliases': []})
    for name in ('bundle.json', 'security.json'):
        os.chown(publish/name, 0, account.pw_gid)
        (publish/name).chmod(0o640)
    monkeypatch.setattr(deployment, 'ROOT', root)
    store = StateStore(tmp_path/'plans')
    directory_fd = os.open(publish, os.O_RDONLY | os.O_DIRECTORY)
    process = None
    constructor = deployment.http.client.HTTPConnection
    route = next(iter_routes(build_bundle([profiles[0]], 'previous')['profiles'][0]))

    def launch():
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
        child = subprocess.Popen([sys.executable, '-c',
            "import runpy,sys; runpy.run_path(sys.argv[1])['_run_unprivileged_publisher'](*sys.argv[2:])",
            str(Path(__file__).resolve()), str(directory_fd), str(account.pw_uid), str(account.pw_gid), str(port)],
            pass_fds=(directory_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return child, port

    def request(port, method):
        client = constructor('127.0.0.1', port, timeout=.3)
        try:
            client.request(method, route['path'], headers={'Host': route['host']})
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    try:
        # A directory fd avoids pytest's private ancestor modes without weakening
        # them. The final publish directory still enforces real uid permissions.
        assert deployment.check_publication_start(store) == {'status': 'publication_safe'}
        process, port = launch()
        deadline = time.monotonic()+5
        while True:
            assert process.poll() is None, process.stderr.read()
            try:
                assert request(port, 'GET') == (200, route['body'].encode())
                break
            except (OSError, deployment.http.client.HTTPException):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        assert json.loads(process.stdout.readline()) == {'uid': account.pw_uid, 'gid': account.pw_gid}
        deployment._publication_gate(closed=True)
        assert stat.S_IMODE(publish.stat().st_mode) == 0o700
        for method in ('GET', 'HEAD'):
            assert request(port, method) == (503, b'')
        # Simulate ExecStart after its root precheck passed but the gate closed.
        prechecked, _ = launch()
        stdout, stderr = prechecked.communicate(timeout=5)
        assert prechecked.returncode == 77, (stdout, stderr)
        advance_security(publish/'security.json', tokens=[profiles[0]['token']])
        deployment._publication_gate(closed=False)
        for method in ('GET', 'HEAD'):
            assert request(port, method) == (404, b'')
    finally:
        if process is not None:
            process.terminate()
            process.communicate(timeout=5)
        os.close(directory_fd)
        store.db.close()
