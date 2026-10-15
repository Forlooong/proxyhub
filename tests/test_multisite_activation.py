"""Multi-site deployment isolation, immutable inputs and web-only recovery."""
from copy import deepcopy
import os
import socket
import stat
import sys
from types import SimpleNamespace

import pytest

from proxyhub import deployment
from proxyhub.core import OperationError, digest
from proxyhub.websites import get_certificate, get_site, normalize_web, site_directory, sync_default
from test_subscription_activation import environment, profile, read_config


SITE = 'b' * 32
CERTIFICATE = 'c' * 32


@pytest.fixture
def multisite(environment, monkeypatch):
    env = environment
    env.observed_health = []
    env.fingerprints, env.certificate_files = {}, {}
    env.real_check_new_ports = deployment._check_new_ports
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_gid=123)))
    monkeypatch.setattr(deployment.os, 'chown', lambda *args: None, raising=False)
    monkeypatch.setattr(deployment, 'render_nginx', lambda *args: 'checked nginx fixture')
    monkeypatch.setattr(deployment, 'client_config', lambda *args: {
        'yaml': profile()['yaml'], 'raw': profile()['raw'],
    })
    monkeypatch.setattr(deployment, 'validate_certificate', lambda certificate, key, domains: {
        'sha256': env.fingerprints.setdefault(certificate, digest(certificate.encode())),
    })
    monkeypatch.setattr(deployment, '_certificate_files', lambda binding: deepcopy(
        env.certificate_files.setdefault(binding['certificate_path'], {
            'certificate_path': digest(binding['certificate_path'].encode()),
            'key_path': digest(binding['key_path'].encode()),
        })))
    monkeypatch.setattr(deployment, '_wait_health', lambda server: None)
    monkeypatch.setattr(deployment, '_wait_web_health', lambda config, directory:
                        env.observed_health.append((deepcopy(config), directory)))
    monkeypatch.setattr(deployment, '_wait_publication', lambda directory: None)
    monkeypatch.setattr(deployment, '_check_new_ports', lambda config, previous: None)
    monkeypatch.setattr(deployment, '_check_new_domains', lambda config, previous: None)
    return env


def two_sites(env):
    config = read_config(env.pointer['current'])
    web = config['web'] = normalize_web(config)
    web['certificates'].append({
        'id': CERTIFICATE, 'name': 'Independent certificate', 'domains': ['second.example.test'],
        'certificate_path': '/var/lib/proxyhub/certificates/' + 'd' * 64 + '/fullchain.pem',
        'key_path': '/var/lib/proxyhub/certificates/' + 'd' * 64 + '/privkey.pem',
    })
    web['sites'].append({
        'id': SITE, 'listener_id': 'site-' + SITE, 'tls_port': 9443,
        'domains': ['second.example.test'], 'title': 'Second <website>', 'enabled': True,
        'certificate_id': CERTIFICATE, 'content_version': None,
    })
    return config


def activate(env, config, **options):
    prepared = deployment.prepare(env.store, config, web_only=True, **options)
    deployment.apply(env.store, prepared['plan_id'])
    return prepared


def test_site_templates_and_web_reload_preserve_proxy_and_subscription(multisite):
    env = multisite
    original = read_config(env.previous)
    prepared = deployment.prepare(env.store, two_sites(env), web_only=True)
    directory = env.root / 'revisions' / prepared['revision']
    assert '欢迎来访'.encode() in (directory / 'site/index.html').read_bytes()
    assert b'Second &lt;website&gt;' in (directory / site_directory(SITE) / 'index.html').read_bytes()
    payload = env.store.inspect(prepared['plan_id'])['payload']
    assert set(payload['certificate_bindings']) == {'default', CERTIFICATE}
    assert site_directory(SITE) + '/index.html' in payload['files']
    env.calls.clear()
    result = deployment.apply(env.store, prepared['plan_id'])
    assert result['activation'] == 'web'
    assert env.calls == [('publish', directory), ('systemctl', 'reload', 'proxyhub-nginx.service'),
                         ('systemctl', 'reload', 'proxyhub-publisher.service')]
    current = read_config(directory)
    assert current['server'] == original['server']
    assert current['subscription'] == original['subscription'] and current['overrides'] == original['overrides']
    assert env.observed_health[-1] == (current, directory)


def test_content_history_is_owned_by_its_site_and_cross_site_restore_is_rejected(multisite):
    env = multisite
    second_files = {'index.html': b'Second upload', 'assets/style.css': b'body {color:blue}'}
    second = activate(env, two_sites(env), site_id=SITE, site_files=second_files, website_change=True)
    default = activate(env, read_config(env.pointer['current']), site_files={'index.html': b'Default upload'},
                       website_change=True)
    current = read_config(env.pointer['current'])
    assert deployment.content_version(env.store, second['revision'], SITE) == second_files
    assert [item['id'] for item in deployment.content_versions(env.store, current, SITE)['versions']] == [
        'template', second['revision'],
    ]
    assert [item['id'] for item in deployment.content_versions(env.store, current)['versions']] == [
        'template', default['revision'],
    ]
    assert (env.pointer['current'] / site_directory(SITE) / 'assets/style.css').read_bytes() == second_files['assets/style.css']
    for version, site_id in ((default['revision'], SITE), (second['revision'], 'default')):
        with pytest.raises(OperationError, match='not been successfully applied'):
            deployment.content_version(env.store, version, site_id)
    get_site(current['web'], SITE)['content_version'] = default['revision']
    before = set((env.root / 'revisions').iterdir())
    with pytest.raises(OperationError, match='not been successfully applied'):
        deployment.prepare(env.store, current, web_only=True, site_id=SITE)
    assert set((env.root / 'revisions').iterdir()) == before


def test_phase2a_default_content_remains_readable_after_resource_upgrade(multisite):
    env = multisite
    historical = env.revision('e')
    content = b'Historical phase 2a website'
    (historical / 'site/index.html').write_bytes(content)
    identity = env.store.plan('deployment', {
        'revision': historical.name, 'content_version': historical.name,
        'website_summary': {'file_count': 1, 'total_bytes': len(content)},
        'files': {name: digest((historical / name).read_bytes()) for name in deployment.CANDIDATE_FILES},
    })
    env.store.record(identity, 'activating', {'previous': str(env.previous), 'candidate': str(historical)})
    env.store.commit(identity)
    config = two_sites(env)
    get_site(config['web'], 'default')['content_version'] = historical.name
    activate(env, sync_default(config))
    current = read_config(env.pointer['current'])
    assert deployment.content_version(env.store, historical.name) == {'index.html': content}
    assert (env.pointer['current'] / 'site/index.html').read_bytes() == content
    assert current['website_content'] == historical.name
    assert [item['id'] for item in deployment.content_versions(env.store, current)['versions']] == ['template', historical.name]
    assert len(deployment.content_versions(env.store, current, SITE)['versions']) == 1


def test_publication_only_cannot_carry_website_resource_changes(multisite):
    env = multisite
    activate(env, two_sites(env))
    config = read_config(env.pointer['current'])
    get_site(config['web'], SITE)['title'] = 'Changed website'
    config['subscription']['name'] = 'New subscription metadata'
    env.calls.clear()
    with pytest.raises(OperationError, match='cannot change website resources'):
        deployment.prepare(env.store, config, publish_only=True)
    assert env.calls == [] and env.store.pending() == []


@pytest.mark.parametrize('section,field,value', [
    ('server', 'node_name', 'Changed proxy'),
    ('subscription', 'name', 'Changed subscription'),
    ('overrides', 'mixed-port', 17891),
])
def test_web_only_cannot_change_other_modules(multisite, section, field, value):
    env = multisite
    config = two_sites(env)
    config[section][field] = value
    env.calls.clear()
    with pytest.raises(OperationError, match='cannot change proxy or subscription'):
        deployment.prepare(env.store, config, web_only=True)
    assert env.calls == [] and env.pointer['current'] == env.previous


@pytest.mark.parametrize('web_only', [True, False])
@pytest.mark.parametrize('endpoint', ['default-sni', 'site-port'])
def test_existing_endpoints_require_migration_in_every_activation_mode(multisite, endpoint, web_only):
    env = multisite
    activate(env, two_sites(env))
    current = env.pointer['current']
    config = read_config(current)
    if endpoint == 'default-sni':
        config['server']['site_domain'] = 'changed.example.test'
        get_site(config['web'], 'default')['domains'].insert(0, 'changed.example.test')
        get_certificate(config['web'], 'default')['domains'].append('changed.example.test')
    else:
        get_site(config['web'], SITE)['tls_port'] = 9445
    env.calls.clear()
    with pytest.raises(OperationError, match='migration stage|cannot change'):
        deployment.prepare(env.store, config, web_only=web_only)
    assert env.calls == [] and env.pointer['current'] == current


@pytest.mark.parametrize('mutation', ['asset', 'missing', 'extra-file', 'extra-site'])
def test_additional_site_tampering_is_refused_before_service_changes(multisite, mutation):
    env = multisite
    prepared = deployment.prepare(env.store, two_sites(env), web_only=True, site_id=SITE,
                                  site_files={'index.html': b'New site', 'assets/style.css': b'body {}'})
    directory = env.root / 'revisions' / prepared['revision']
    tree = directory / site_directory(SITE)
    if mutation == 'asset':
        (tree / 'assets/style.css').write_bytes(b'tampered')
    elif mutation == 'missing':
        (tree / 'assets/style.css').unlink()
    elif mutation == 'extra-file':
        (tree / 'extra.txt').write_bytes(b'unlisted')
    else:
        (directory / 'sites' / ('f' * 32)).mkdir()
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.apply(env.store, prepared['plan_id'])
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


@pytest.mark.parametrize('certificate_id,mutation', [
    ('default', 'files'), (CERTIFICATE, 'files'), ('f' * 32, 'files'),
    ('default', 'fingerprint'), (CERTIFICATE, 'fingerprint'),
])
def test_every_certificate_snapshot_is_rechecked_including_unused_bindings(multisite, certificate_id, mutation):
    env = multisite
    config = two_sites(env)
    if certificate_id == 'f' * 32:
        config['web']['certificates'].append({
            'id': certificate_id, 'name': 'Unused certificate', 'domains': ['unused.example.test'],
            'certificate_path': '/var/lib/proxyhub/certificates/87654321/fullchain.pem',
            'key_path': '/var/lib/proxyhub/certificates/87654321/privkey.pem',
        })
    prepared = deployment.prepare(env.store, config, web_only=True)
    path = get_certificate(config['web'], certificate_id)['certificate_path']
    if mutation == 'files':
        env.certificate_files[path]['key_path'] = 'tampered private key digest'
    else:
        env.fingerprints[path] = '0' * 64
    env.calls.clear()
    with pytest.raises(OperationError, match='certificate binding changed'):
        deployment.apply(env.store, prepared['plan_id'])
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


def test_new_consumer_health_failure_restores_all_old_web_state_without_history(multisite, monkeypatch):
    env = multisite
    activate(env, two_sites(env))
    previous = env.pointer['current']
    original = read_config(previous)
    config = deepcopy(original)
    certificate = get_certificate(config['web'], CERTIFICATE)
    certificate.update(certificate_path='/var/lib/proxyhub/certificates/87654321/fullchain.pem',
                       key_path='/var/lib/proxyhub/certificates/87654321/privkey.pem')
    prepared = deployment.prepare(env.store, config, web_only=True, site_id=SITE,
                                  site_files={'index.html': b'Failed candidate'})
    candidate = env.root / 'revisions' / prepared['revision']
    probes = []
    def health(snapshot, directory):
        probes.append((deepcopy(snapshot), directory))
        if directory == candidate:
            raise OperationError('New certificate not loaded')
        assert snapshot == original
        for website in normalize_web(snapshot)['sites']:
            assert (directory / site_directory(website['id']) / 'index.html').read_bytes() == (
                previous / site_directory(website['id']) / 'index.html').read_bytes()
    monkeypatch.setattr(deployment, '_wait_web_health', health)
    env.calls.clear()
    with pytest.raises(OperationError, match='not loaded'):
        deployment.apply(env.store, prepared['plan_id'])
    assert [directory for _, directory in probes] == [candidate, previous]
    assert env.pointer['current'] == previous and read_config(previous) == original
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed' and env.store.pending() == []
    assert not any('restart' in call for call in env.calls)
    with pytest.raises(OperationError, match='not been successfully applied'):
        deployment.content_version(env.store, prepared['revision'], SITE)
    assert len(deployment.content_versions(env.store, original, SITE)['versions']) == 1


def test_interrupted_web_activation_recovers_previous_consumers_without_proxy_restart(multisite):
    env = multisite
    activate(env, two_sites(env))
    previous = env.pointer['current']
    prepared = deployment.prepare(env.store, read_config(previous), web_only=True, site_id=SITE,
                                  site_files={'index.html': b'Interrupted content'})
    candidate = env.root / 'revisions' / prepared['revision']
    env.store.record(prepared['plan_id'], 'activating', {
        'previous': str(previous), 'candidate': str(candidate), 'activation': 'web',
    })
    env.pointer['current'] = candidate
    env.calls.clear()
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == previous and env.store.pending() == []
    assert env.observed_health[-1] == (read_config(previous), previous)
    assert not any('restart' in call for call in env.calls)
    assert deployment.recover(env.store)['status'] == 'nothing_to_recover'
    with pytest.raises(OperationError, match='not been successfully applied'):
        deployment.content_version(env.store, prepared['revision'], SITE)


def test_new_port_taken_after_preparation_is_refused_before_service_changes(multisite, monkeypatch):
    env = multisite
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(('0.0.0.0', 0))
        occupied.listen()
        config = two_sites(env)
        get_site(config['web'], SITE)['tls_port'] = occupied.getsockname()[1]
        prepared = deployment.prepare(env.store, config, web_only=True)
        monkeypatch.setattr(deployment, '_check_new_ports', env.real_check_new_ports)
        env.calls.clear()
        with pytest.raises(OperationError, match='unavailable; no service was replaced'):
            deployment.apply(env.store, prepared['plan_id'])
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


def test_raw_deployment_cannot_bypass_enabled_site_deletion_guard(multisite):
    env = multisite
    activate(env,two_sites(env))
    config = read_config(env.pointer['current'])
    config['web']['sites'] = [site for site in config['web']['sites'] if site['id']!='b'*32]
    with pytest.raises(OperationError,match='disabled before deletion'):
        deployment.prepare(env.store,config,web_only=True)


def test_raw_deployment_cannot_drop_a_referenced_certificate_binding(multisite):
    env = multisite
    activate(env,two_sites(env))
    config = read_config(env.pointer['current'])
    config['web']['certificates'][0]['domains'].append('second.example.test')
    get_site(config['web'],SITE)['certificate_id'] = 'default'
    config['web']['certificates'] = [item for item in config['web']['certificates'] if item['id']!=CERTIFICATE]
    with pytest.raises(OperationError,match='referenced certificate'):
        deployment.prepare(env.store,config,web_only=True)


@pytest.mark.skipif(os.name != 'posix', reason='Real Unix directory modes and umask')
def test_multisite_assets_keep_runtime_permissions_under_private_manager_umask(multisite):
    env = multisite
    previous_mask = os.umask(0o077)
    try:
        prepared = deployment.prepare(env.store, two_sites(env), web_only=True, site_id=SITE,
                                      site_files={'index.html': b'New site', 'a/b/style.css': b'body {}'})
    finally:
        os.umask(previous_mask)
    directory = env.root / 'revisions' / prepared['revision']
    deployment._check_runtime_permissions(directory)
    for relative in ('site', 'sites', site_directory(SITE), site_directory(SITE) + '/a/b'):
        assert stat.S_IMODE((directory / relative).stat().st_mode) == 0o755
    assert stat.S_IMODE((directory / site_directory(SITE) / 'a/b/style.css').stat().st_mode) == 0o644
    assert stat.S_IMODE((directory / 'config.json').stat().st_mode) == 0o600
