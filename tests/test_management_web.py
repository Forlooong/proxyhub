"""Website operations protect REALITY, certificate consumers and private input."""
from copy import deepcopy
import ipaddress
import json
import socket
from types import SimpleNamespace

import pytest

from proxyhub import certificates, management_web
from proxyhub.management_errors import ManagementError
from proxyhub.proxy import validate_spec
from proxyhub.websites import get_certificate, get_site, normalize_web
from test_deployment import deployment_spec

SITE, CERTIFICATE = 'b' * 32, 'c' * 32


@pytest.fixture
def configured(monkeypatch):
    config = {'server': validate_spec(deployment_spec()), 'website_content': 'a' * 32}
    config['web'] = web = normalize_web(config)
    web['certificates'].append({
        'id': CERTIFICATE, 'name': 'Second certificate',
        'domains': ['second.example.test', 'alias.example.test'],
        'certificate_path': '/var/lib/proxyhub/certificates/' + 'd' * 64 + '/fullchain.pem',
        'key_path': '/var/lib/proxyhub/certificates/' + 'd' * 64 + '/privkey.pem',
    })
    web['sites'].append({'id': SITE, 'listener_id': 'site-' + SITE, 'tls_port': 8445,
                         'domains': ['second.example.test'], 'title': 'Second website',
                         'enabled': True, 'certificate_id': CERTIFICATE, 'content_version': None})
    monkeypatch.setattr(management_web, 'check_domain', lambda *args: None)
    monkeypatch.setattr(management_web, 'validate_certificate', lambda *args: {'domains': args[2]})
    return config


def change(config, kind, values, tmp_path):
    return management_web.change(config, kind, values, tmp_path)


def test_create_gets_own_listener_free_port_and_template_without_mutating_input(configured, tmp_path):
    before = deepcopy(configured)
    result = change(configured, 'website.create', {'title': 'Third', 'domain': 'alias.example.test',
                                                   'certificate_id': CERTIFICATE}, tmp_path)
    created = result['config']['web']['sites'][-1]
    assert len(created['id']) == 32 and created['id'] != SITE
    assert created['listener_id'] == 'site-' + created['id']
    assert created['tls_port'] == 8446 and created['content_version'] is None
    assert created['enabled'] is True
    assert configured == before


@pytest.mark.parametrize('kind,values', [
    ('website.disable', {'site_id': 'default'}),
    ('website.delete', {'site_id': 'default'}),
    ('website.domain.unbind', {'site_id': 'default', 'domain': 'site.example.test'}),
])
def test_reality_references_block_destructive_default_changes(configured, tmp_path, kind, values):
    with pytest.raises(ManagementError, match='REALITY.*site.example.test') as caught:
        change(configured, kind, values, tmp_path)
    assert caught.value.code == 'DEPENDENCY'


@pytest.fixture
def migrating(configured):
    get_site(configured['web'], SITE)['domains'].append('alias.example.test')
    configured['reality'] = {
        'targets': [
            {'id': 'default', 'type': 'site', 'site_id': 'default', 'server_name': 'site.example.test'},
            {'id': '1' * 32, 'type': 'site', 'site_id': SITE, 'server_name': 'second.example.test'},
        ],
        'endpoints': [
            {'id': 'default', 'listener_id': 'reality-default', 'listen_port': 2443, 'target_id': 'default'},
            {'id': '2' * 32, 'listener_id': 'reality-' + '2' * 32,
             'listen_port': 2444, 'target_id': '1' * 32},
        ],
        'primary_endpoint_id': 'default',
    }
    return configured


@pytest.mark.parametrize('kind,values', [
    ('website.disable', {'site_id': SITE}),
    ('website.delete', {'site_id': SITE}),
    ('website.domain.unbind', {'site_id': SITE, 'domain': 'second.example.test'}),
])
def test_retained_nonprimary_reality_target_protects_its_site_and_sni(migrating, tmp_path, kind, values):
    with pytest.raises(ManagementError) as caught:
        change(migrating, kind, values, tmp_path)
    assert caught.value.code == 'DEPENDENCY'
    assert '2' * 32 in str(caught.value) and 'second.example.test' in str(caught.value)


def test_reality_reference_allows_unrelated_alias_changes_and_ends_on_explicit_retirement(migrating, tmp_path):
    unbound = change(migrating, 'website.domain.unbind', {'site_id': SITE, 'domain': 'alias.example.test'}, tmp_path)['config']
    assert get_site(unbound['web'], SITE)['domains'] == ['second.example.test']
    rebound = change(unbound, 'website.domain.bind', {'site_id': SITE, 'domain': 'alias.example.test'}, tmp_path)['config']
    assert get_site(rebound['web'], SITE)['domains'] == ['second.example.test', 'alias.example.test']
    rebound['reality']['targets'] = rebound['reality']['targets'][:1]
    rebound['reality']['endpoints'] = rebound['reality']['endpoints'][:1]
    disabled = change(rebound, 'website.disable', {'site_id': SITE}, tmp_path)['config']
    assert get_site(disabled['web'], SITE)['enabled'] is False


@pytest.mark.parametrize('kind,values', [
    ('website.disable', {'site_id': 'default'}),
    ('website.delete', {'site_id': 'default'}),
    ('website.domain.unbind', {'site_id': 'default', 'domain': 'site.example.test'}),
])
def test_unreferenced_default_preserves_compatibility_without_claiming_reality_dependency(migrating, tmp_path, kind, values):
    reality = migrating['reality']
    reality['targets'] = reality['targets'][1:]
    reality['endpoints'] = reality['endpoints'][1:]
    reality['primary_endpoint_id'] = '2' * 32
    with pytest.raises(ManagementError) as caught:
        change(migrating, kind, values, tmp_path)
    assert caught.value.code == 'DEPENDENCY'
    assert '兼容' in str(caught.value) and 'REALITY' not in str(caught.value)


def test_enabled_site_delete_and_last_domain_unbind_require_disabling(configured, tmp_path):
    for kind, values in [('website.delete', {'site_id': SITE}),
                         ('website.domain.unbind', {'site_id': SITE, 'domain': 'second.example.test'})]:
        with pytest.raises(ManagementError, match='Second website HTTPS'):
            change(configured, kind, values, tmp_path)
    disabled = change(configured, 'website.disable', {'site_id': SITE}, tmp_path)['config']
    empty = change(disabled, 'website.domain.unbind', {'site_id': SITE, 'domain': 'second.example.test'}, tmp_path)['config']
    assert get_site(empty['web'], SITE)['domains'] == []
    with pytest.raises(ManagementError, match='域名与有效证书'):
        change(empty, 'website.enable', {'site_id': SITE}, tmp_path)
    deleted = change(empty, 'website.delete', {'site_id': SITE}, tmp_path)['config']
    assert [site['id'] for site in deleted['web']['sites']] == ['default']


def test_certificate_delete_reports_disabled_website_subscription_and_reality_consumers(configured, tmp_path):
    disabled = change(configured, 'website.disable', {'site_id': SITE}, tmp_path)['config']
    empty = change(disabled, 'website.domain.unbind', {'site_id': SITE, 'domain': 'second.example.test'}, tmp_path)['config']
    with pytest.raises(ManagementError, match='Second website HTTPS'):
        change(empty, 'certificate.delete', {'certificate_id': CERTIFICATE}, tmp_path)
    with pytest.raises(ManagementError) as caught:
        change(configured, 'certificate.delete', {'certificate_id': 'default'}, tmp_path)
    assert '订阅 HTTPS' in str(caught.value) and 'REALITY' in str(caught.value)
    removed = change(disabled, 'website.delete', {'site_id': SITE}, tmp_path)['config']
    removed = change(removed, 'certificate.delete', {'certificate_id': CERTIFICATE}, tmp_path)['config']
    assert [certificate['id'] for certificate in removed['web']['certificates']] == ['default']


@pytest.mark.parametrize('enabled', [True, False])
def test_hy2_blocks_certificate_deletion_after_website_removal(configured, tmp_path, enabled):
    configured['hy2'] = {'id': 'local-hy2', 'listener_id': 'hy2-default', 'enabled': enabled,
                         'name': 'Independent node', 'port': 443, 'server_name': 'alias.example.test',
                         'certificate_id': CERTIFICATE, 'password': 'not-public'}
    disabled = change(configured, 'website.disable', {'site_id': SITE}, tmp_path)['config']
    removed = change(disabled, 'website.delete', {'site_id': SITE}, tmp_path)['config']
    with pytest.raises(ManagementError, match='Independent node HY2') as caught:
        change(removed, 'certificate.delete', {'certificate_id': CERTIFICATE}, tmp_path)
    assert caught.value.code == 'DEPENDENCY'
    assert 'not-public' not in str(caught.value)
    assert removed['hy2'] == configured['hy2']


def test_binding_checks_actual_certificate_even_on_disabled_empty_site(configured, tmp_path, monkeypatch):
    site = get_site(configured['web'], SITE)
    site.update(enabled=False, domains=[])
    def reject(*args):
        raise ValueError('private path and secret diagnostics')
    monkeypatch.setattr(management_web, 'validate_certificate', reject)
    with pytest.raises(ManagementError, match='实际 SAN') as caught:
        change(configured, 'website.certificate.bind', {'site_id': SITE, 'certificate_id': CERTIFICATE}, tmp_path)
    assert 'private path' not in str(caught.value)


def test_domain_bind_and_enable_check_dns_and_actual_sans(configured, tmp_path, monkeypatch):
    checked = []
    monkeypatch.setattr(management_web, 'check_domain', lambda server, domain: checked.append(domain))
    validated = []
    monkeypatch.setattr(management_web, 'validate_certificate', lambda cert, key, domains: validated.append(domains))
    bound = change(configured, 'website.domain.bind', {'site_id': SITE, 'domain': 'alias.example.test'}, tmp_path)['config']
    assert checked == ['alias.example.test']
    assert validated == [['second.example.test', 'alias.example.test']]
    disabled = change(bound, 'website.disable', {'site_id': SITE}, tmp_path)['config']
    change(disabled, 'website.enable', {'site_id': SITE}, tmp_path)
    assert checked == ['alias.example.test', 'second.example.test', 'alias.example.test']


def test_rebinding_default_preserves_subscription_binding_and_projects_legacy_paths(configured, tmp_path):
    get_certificate(configured['web'], CERTIFICATE)['domains'].append('site.example.test')
    result = change(configured, 'website.certificate.bind', {'site_id': 'default', 'certificate_id': CERTIFICATE}, tmp_path)['config']
    assert result['server']['certificate_path'] == get_certificate(result['web'], CERTIFICATE)['certificate_path']
    assert result['web']['subscription_certificate_id'] == 'default'
    assert result['website_content'] == configured['website_content']


@pytest.mark.parametrize('kind,values', [
    ('website.create', {'title': 'Site', 'domain': 'alias.example.test', 'certificate_id': CERTIFICATE, 'tls_port': 443}),
    ('website.disable', {'site_id': SITE, 'enabled': False}),
    ('website.delete', {'site_id': '../private-path'}),
    ('website.domain.bind', {'site_id': SITE, 'domain': '*.example.test'}),
    ('website.certificate.bind', {'site_id': SITE, 'certificate_id': None}),
    ('certificate.add', {'name': 'Missing fields'}),
    ('certificate.import', {'certificate_pem': 'pem', 'private_key_pem': 'pem', 'domains': []}),
    ('certificate.delete', {'certificate_id': 'unknown'}),
])
def test_strict_input_validation(configured, tmp_path, kind, values):
    before = deepcopy(configured)
    with pytest.raises(ManagementError):
        change(configured, kind, values, tmp_path)
    assert configured == before


def test_import_targets_stable_binding_and_requires_all_declared_and_consumer_domains(configured, tmp_path, monkeypatch):
    imported = []
    def import_pair(cert, key, domains):
        assert cert.read_text() == 'PUBLIC' and key.read_text() == 'PRIVATE-TEST-INPUT'
        imported.append(domains)
        return {'certificate_path': '/var/lib/proxyhub/certificates/' + 'e' * 64 + '/fullchain.pem',
                'key_path': '/var/lib/proxyhub/certificates/' + 'e' * 64 + '/privkey.pem',
                'sha256': 'f' * 64, 'not_after': '2030-01-01'}
    monkeypatch.setattr(certificates, 'import_pair', import_pair)
    original = deepcopy(configured)
    result = change(configured, 'certificate.import', {'certificate_id': CERTIFICATE,
                    'certificate_pem': 'PUBLIC', 'private_key_pem': 'PRIVATE-TEST-INPUT'}, tmp_path)
    assert imported == [['second.example.test', 'alias.example.test']]
    assert get_certificate(result['config']['web'], CERTIFICATE)['certificate_path'].endswith('e' * 64 + '/fullchain.pem')
    assert get_certificate(result['config']['web'], 'default') == get_certificate(original['web'], 'default')
    assert 'PRIVATE-TEST-INPUT' not in json.dumps(result)
    assert list((tmp_path/'certificates').iterdir()) == []
    result = change(configured, 'certificate.import', {'certificate_pem': 'PUBLIC', 'private_key_pem': 'PRIVATE-TEST-INPUT'}, tmp_path)
    assert imported[-1] == ['site.example.test', 'sub.example.test']
    assert result['config']['server']['certificate_path'].endswith('e' * 64 + '/fullchain.pem')
    assert configured == original


def test_add_certificate_has_stable_identity_without_dns_or_pem_in_preview(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(management_web, 'check_domain', lambda *args: pytest.fail('Unbound certificates need no DNS check'))
    monkeypatch.setattr(certificates, 'import_pair', lambda cert, key, domains: {
        'certificate_path': '/var/lib/proxyhub/certificates/' + 'e' * 64 + '/fullchain.pem',
        'key_path': '/var/lib/proxyhub/certificates/' + 'e' * 64 + '/privkey.pem'})
    result = change(configured, 'certificate.add', {'name': 'Future', 'domains': ['future.example.test'],
                    'certificate_pem': 'PUBLIC', 'private_key_pem': 'PRIVATE-TEST-INPUT'}, tmp_path)
    added = result['config']['web']['certificates'][-1]
    assert len(added['id']) == 32 and added['id'] != CERTIFICATE
    assert added['domains'] == ['future.example.test']
    assert 'PRIVATE-TEST-INPUT' not in json.dumps(result)
    assert '不接管自动续期' in result['warnings'][0]


def test_dns_requires_every_a_and_aaaa_and_accepts_local_global_interface(monkeypatch):
    monkeypatch.setattr(management_web, '_local_addresses', lambda: {ipaddress.ip_address('2001:4860:4860::8888')})
    def resolve(host, *args):
        assert host == 'site.example.test'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.10', 0)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001:4860:4860::8888', 0, 0, 0))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    management_web.check_domain({'public_address': '203.0.113.10'}, 'site.example.test')
    monkeypatch.setattr(management_web, '_local_addresses', lambda: set())
    with pytest.raises(ManagementError, match='全部 A / AAAA'):
        management_web.check_domain({'public_address': '203.0.113.10'}, 'site.example.test')


@pytest.mark.parametrize('answers', [[], ['198.51.100.20'], ['203.0.113.10', '127.0.0.1']])
def test_dns_empty_other_host_or_mixed_private_record_is_rejected(monkeypatch, answers):
    monkeypatch.setattr(management_web, '_local_addresses', lambda: set())
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 0)) for address in answers])
    with pytest.raises(ManagementError) as caught:
        management_web.check_domain({'public_address': '203.0.113.10'}, 'site.example.test')
    assert caught.value.code == 'DNS_CHECK_FAILED'


def test_dns_compares_configured_host_resolution_without_http_requests(monkeypatch):
    hosts = []
    def resolve(host, *args):
        hosts.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.10', 0))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(management_web, '_local_addresses', lambda: set())
    management_web.check_domain({'public_address': 'server.example.test'}, 'site.example.test')
    assert hosts == ['site.example.test', 'server.example.test']


def test_local_address_inspection_excludes_loopback_private_and_link_local(monkeypatch):
    payload = [{'addr_info': [{'local': '127.0.0.1', 'scope': 'host'},
                            {'local': '192.168.1.2', 'scope': 'global'},
                            {'local': 'fe80::1', 'scope': 'link'},
                            {'local': '2001:4860:4860::8888', 'scope': 'global'}]}]
    monkeypatch.setattr(management_web.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode()))
    assert management_web._local_addresses() == {ipaddress.ip_address('2001:4860:4860::8888')}


def test_disabled_independent_protocol_node_protects_certificate_binding(configured, tmp_path):
    from proxyhub import nodes
    config = nodes.change(configured, 'nodes.create', {
        'protocol': 'anytls', 'name': 'Kept AnyTLS', 'port': 20001,
        'options': {'sni': 'alias.example.test', 'certificate_id': CERTIFICATE}})
    node = config['nodes'][0]
    node['enabled'] = False
    get_site(config['web'], SITE).update(enabled=False, domains=[], certificate_id=None)
    with pytest.raises(ManagementError, match='Kept AnyTLS') as caught:
        change(config, 'certificate.delete', {'certificate_id': CERTIFICATE}, tmp_path)
    assert caught.value.code == 'DEPENDENCY'
    assert node['credentials']['password'] not in str(caught.value)
    config['nodes'] = []
    deleted = change(config, 'certificate.delete', {'certificate_id': CERTIFICATE}, tmp_path)
    assert all(certificate['id'] != CERTIFICATE for certificate in deleted['config']['web']['certificates'])


@pytest.mark.parametrize('kind,values', [
    ('website.disable', {'site_id': SITE}),
    ('website.delete', {'site_id': SITE}),
    ('website.domain.unbind', {'site_id': SITE, 'domain': 'second.example.test'}),
])
def test_disabled_protocol_reality_node_is_named_in_site_dependency(migrating, tmp_path, kind, values):
    from proxyhub import nodes
    config = nodes.change(migrating, 'nodes.create', {
        'protocol': 'vless', 'name': 'Kept VLESS', 'port': 20001,
        'options': {'tls_mode': 'reality', 'sni': 'second.example.test', 'reality_target_id': '1' * 32}})
    config['nodes'][0]['enabled'] = False
    with pytest.raises(ManagementError) as caught:
        change(config, kind, values, tmp_path)
    assert caught.value.code == 'DEPENDENCY'
    assert config['nodes'][0]['id'] in str(caught.value) and 'Kept VLESS' in str(caught.value)


def test_protocol_certificate_replacement_requires_every_node_sni(configured, tmp_path, monkeypatch):
    from proxyhub import nodes
    certificate = get_certificate(configured['web'], CERTIFICATE)
    certificate['domains'].append('future.example.test')
    config = nodes.change(configured, 'nodes.create', {
        'protocol': 'anytls', 'name': 'Future AnyTLS', 'port': 20001,
        'options': {'sni': 'future.example.test', 'certificate_id': CERTIFICATE}})
    config['nodes'][0]['enabled'] = False
    get_certificate(config['web'], CERTIFICATE)['domains'].remove('future.example.test')
    checked = []
    def imported(values, required, root):
        checked.extend(required)
        return {'certificate_path': certificate['certificate_path'], 'key_path': certificate['key_path'],
                'sha256': 'e' * 64, 'not_after': '2030-01-01'}
    monkeypatch.setattr(management_web, '_import', imported)
    change(config, 'certificate.import', {'certificate_id': CERTIFICATE,
            'certificate_pem': 'PUBLIC', 'private_key_pem': 'PRIVATE'}, tmp_path)
    assert checked == ['second.example.test', 'alias.example.test', 'future.example.test']


def test_website_allocator_skips_retained_protocol_tcp_port(configured, tmp_path):
    from proxyhub import nodes
    config = nodes.change(configured, 'nodes.create', {
        'protocol': 'socks5', 'name': 'Kept SOCKS', 'port': 8446, 'options': {}})
    config['nodes'][0]['enabled'] = False
    result = change(config, 'website.create', {'title': 'Third', 'domain': 'alias.example.test',
                    'certificate_id': CERTIFICATE}, tmp_path)
    assert result['config']['web']['sites'][-1]['tls_port'] == 8447
