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


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    (root / 'revisions').mkdir(parents=True)
    (root / 'publish').mkdir()
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
    constructor = deployment.http.client.HTTPConnection
    requests = []
    def connection(host,port,**kwargs):
        assert (host,port)==('127.0.0.1',18890)
        return constructor(host,server.server_port,**kwargs)
    def publish(directory):
        env.calls.append(('publish',directory))
        save_bundle(runtime_bundle,json.loads((directory/'bundle.json').read_text(encoding='utf-8')))
    def run(command,**kwargs):
        env.calls.append(tuple(command))
        if command==['systemctl','reload','proxyhub-publisher.service']:
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
    yield SimpleNamespace(env=env,server=server,failure=failure,requests=requests,
                          publish=publish,request=request,runtime_bundle=runtime_bundle)
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

    def checked_health(path):
        health(path)
        if fault['health']:
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


def test_recovery_checks_previous_manifest_before_retrying_publication_safety(environment, monkeypatch):
    env = environment
    directory, identity = env.plan(profiles=[profile(token='U' * 43)], subscription_operation={
        'kind': 'subscription.token.rotate', 'subscription_id': 'default'})
    details = {'previous': str(env.previous), 'candidate': str(directory), 'activation': 'publication',
               'publication_safety_failed': True,
               'previous_files': {path.relative_to(env.previous).as_posix(): digest(path.read_bytes())
                                  for path in env.previous.rglob('*') if path.is_file()},
               'runtime_units_before': [], 'runtime_units_after': []}
    env.store.record(identity, 'activating', details)
    damaged = read_config(env.previous)
    damaged['subscription']['token'] = 'V' * 43
    (env.previous/'config.json').write_bytes(canonical(damaged))
    effects = []
    monkeypatch.setattr(deployment, '_revoke_publication_changes', lambda *args: effects.append('revoke'))
    monkeypatch.setattr(deployment, '_wait_publication', lambda *args: effects.append('acknowledge'))
    with pytest.raises(OperationError, match='changed after validation'):
        deployment.recover(env.store)
    assert effects == [] and env.calls == []
    assert load_security(env.root/'publish/security.json')['generation'] == 0
    assert len(env.store.pending()) == 1 and env.store.inspect(identity)['status'] == 'prepared'
