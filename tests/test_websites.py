"""Pure resource compatibility, dependency and multi-site Nginx contracts."""
from copy import deepcopy

import pytest

from proxyhub.site import render_nginx
from proxyhub.proxy import validate_spec
from proxyhub.websites import (consumers, get_certificate, get_site, normalize_web,
                               site_dependencies, site_directory, sync_default)
from test_deployment import REVISION, deployment_spec


SITE = 'b' * 32
CERTIFICATE = 'c' * 32


@pytest.fixture
def configured():
    config = {'server': validate_spec(deployment_spec()), 'website_content': 'a' * 32}
    web = config['web'] = normalize_web(config)
    web['certificates'].append({
        'id': CERTIFICATE, 'name': 'Second certificate',
        'domains': ['second.example.test', 'alias.example.test', 'future.example.test'],
        'certificate_path': '/var/lib/proxyhub/certificates/' + 'd' * 64 + '/fullchain.pem',
        'key_path': '/var/lib/proxyhub/certificates/' + 'd' * 64 + '/privkey.pem',
    })
    web['sites'].append({
        'id': SITE, 'listener_id': 'site-' + SITE, 'tls_port': 9443,
        'domains': ['second.example.test', 'alias.example.test'], 'title': 'Second website',
        'enabled': True, 'certificate_id': CERTIFICATE, 'content_version': None,
    })
    return config


def test_legacy_defaults_preserve_identity_content_and_input():
    config = {'server': deployment_spec(), 'website_content': 'a' * 32}
    original = deepcopy(config)
    web = normalize_web(config)
    default = get_site(web, 'default')
    assert default['listener_id'] == 'site-default'
    assert default['domains'] == ['site.example.test']
    assert default['content_version'] == 'a' * 32
    assert default['tls_port'] == 8443
    assert web['subscription_certificate_id'] == 'default'
    assert get_certificate(web, 'default')['domains'] == ['site.example.test', 'sub.example.test']
    assert config == original
    default['domains'].append('new.example.test')
    assert config == original
    assert site_directory('default') == 'site'
    assert site_directory(SITE) == 'sites/' + SITE


def test_default_projection_keeps_split_bindings_and_only_changes_legacy_fields(configured):
    original = deepcopy(configured)
    default = get_site(configured['web'], 'default')
    default.update(title='Updated', certificate_id=CERTIFICATE, content_version=None)
    get_certificate(configured['web'], CERTIFICATE)['domains'].append('site.example.test')
    result = sync_default(configured)
    assert result['server']['site_title'] == 'Updated'
    assert result['server']['certificate_path'] == get_certificate(result['web'], CERTIFICATE)['certificate_path']
    assert 'website_content' not in result
    assert result['web']['subscription_certificate_id'] == 'default'
    assert configured['server'] == original['server']
    assert configured['website_content'] == original['website_content']
    default['content_version'] = 'e' * 32
    assert sync_default(configured)['website_content'] == 'e' * 32


@pytest.mark.parametrize('mutate', [
    lambda web: web.update(unknown=True),
    lambda web: web.update(sites=[]),
    lambda web: web.update(certificates=[]),
    lambda web: web['sites'].append(deepcopy(web['sites'][0])),
    lambda web: web['certificates'].append(deepcopy(web['certificates'][0])),
    lambda web: web['sites'][1].update(unknown=True),
    lambda web: web['certificates'][1].update(unknown=True),
    lambda web: web['sites'][1].update(id='../escape'),
    lambda web: web['sites'][1].update(listener_id='site-default'),
    lambda web: web['sites'][1].update(enabled=1),
    lambda web: web['sites'][1].update(title=''),
    lambda web: web['sites'][1].update(domains=[]),
    lambda web: web['sites'][1].update(domains=['second.example.test', 'SECOND.EXAMPLE.TEST']),
    lambda web: web['sites'][1].update(domains=['site.example.test'], enabled=False),
    lambda web: web['sites'][1].update(domains=['sub.example.test']),
    lambda web: web['sites'][1].update(domains=['example.test;root']),
    lambda web: web['sites'][1].update(certificate_id=None),
    lambda web: web['sites'][1].update(certificate_id='e' * 32),
    lambda web: web['sites'][1].update(content_version='not-a-version'),
    lambda web: web['sites'][1].update(tls_port=True),
    lambda web: web['sites'][1].update(tls_port=65536),
    lambda web: web['sites'][1].update(tls_port=8443),
    lambda web: web['sites'][1].update(tls_port=18890),
    lambda web: web['sites'][0].update(enabled=False),
    lambda web: web['sites'][0].update(tls_port=9444),
    lambda web: web['sites'][0].update(domains=['other.example.test']),
    lambda web: web.update(sites=[web['sites'][1]]),
    lambda web: web['certificates'][1].update(domains=['other.example.test']),
    lambda web: web['certificates'][1].update(domains=['second.example.test'] * 11),
    lambda web: web['certificates'][1].update(certificate_path='/etc/ssl/certificate.pem'),
    lambda web: web['certificates'][1].update(key_path='/var/lib/proxyhub/certificates/87654321/privkey.pem'),
    lambda web: web.update(subscription_certificate_id=CERTIFICATE),
])
def test_invalid_resources_are_rejected(configured, mutate):
    mutate(configured['web'])
    with pytest.raises(ValueError):
        normalize_web(configured)


@pytest.mark.parametrize('field,maximum', [('sites', 16), ('certificates', 32)])
def test_resource_counts_are_bounded(configured, field, maximum):
    configured['web'][field] *= maximum
    with pytest.raises(ValueError):
        normalize_web(configured)


def test_disabled_bindings_remain_dependencies_and_can_be_explicitly_cleared(configured):
    second = get_site(configured['web'], SITE)
    second['enabled'] = False
    web = normalize_web(configured)
    dependencies = consumers(web, configured['server'], CERTIFICATE)
    assert len(dependencies) == 2
    assert all(item['kind'] == 'website' and item['enabled'] is False for item in dependencies)
    second['domains'] = []
    dependencies = consumers(normalize_web(configured), configured['server'], CERTIFICATE)
    assert len(dependencies) == 1 and dependencies[0]['domain'] is None
    second['certificate_id'] = None
    assert consumers(normalize_web(configured), configured['server'], CERTIFICATE) == []
    assert site_dependencies(web, configured['server'], SITE) == []
    assert 'REALITY' in site_dependencies(web, configured['server'], 'default')[0]
    assert {item['kind'] for item in consumers(web, configured['server'], 'default')} == {
        'website', 'subscription', 'reality',
    }


def test_resource_ids_do_not_depend_on_domain_or_certificate_material(configured):
    original = deepcopy(configured)
    second = get_site(configured['web'], SITE)
    second['domains'] = ['renamed.example.test']
    certificate = get_certificate(configured['web'], CERTIFICATE)
    certificate.update(domains=['renamed.example.test'],
                       certificate_path='/var/lib/proxyhub/certificates/12345678/fullchain.pem',
                       key_path='/var/lib/proxyhub/certificates/12345678/privkey.pem')
    normalized = normalize_web(configured)
    assert get_site(normalized, SITE)['listener_id'] == get_site(original['web'], SITE)['listener_id']
    assert get_site(normalized, SITE)['certificate_id'] == CERTIFICATE
    assert get_certificate(normalized, CERTIFICATE)['id'] == CERTIFICATE
    get_site(normalized, SITE)['domains'].append('independent.example.test')
    assert second['domains'] == ['renamed.example.test']


def test_nginx_keeps_original_reality_path_and_renders_independent_sites(configured):
    web = configured['web']
    default = get_site(web, 'default')
    default['domains'].append('default-alias.example.test')
    get_certificate(web, 'default')['domains'].append('default-alias.example.test')
    output = render_nginx(configured['server'], REVISION, web)
    assert 'site.example.test 127.0.0.1:2443;' in output
    assert 'default-alias.example.test 127.0.0.1:8443;' in output
    assert 'second.example.test 127.0.0.1:9443;' in output
    assert 'alias.example.test 127.0.0.1:9443;' in output
    assert 'sub.example.test 127.0.0.1:8444;' in output
    assert 'default 127.0.0.1:1;' in output
    assert 'server_name second.example.test alias.example.test;' in output
    assert 'root ' + REVISION + '/sites/' + SITE + ';' in output
    assert 'root ' + REVISION + '/site;' in output
    second_tls = output[output.index('listen 127.0.0.1:9443 ssl;'):]
    assert 'ssl_certificate ' + get_certificate(web, CERTIFICATE)['certificate_path'] + ';' in second_tls
    subscription_tls = output[output.index('listen 127.0.0.1:8444 ssl;'):]
    assert 'ssl_certificate ' + get_certificate(web, 'default')['certificate_path'] + ';' in subscription_tls
    challenge = output[output.index('server_name future.example.test;'):].split('    server {', 1)[0]
    assert 'location ^~ /.well-known/acme-challenge/' in challenge
    assert 'location / { return 404; }' in challenge
    assert 'future.example.test 127.0.0.1:' not in output
    assert "default-src 'none'" in output and 'text/css css;' in output


def test_nginx_disabled_site_keeps_challenge_without_https_listener(configured):
    get_site(configured['web'], SITE)['enabled'] = False
    output = render_nginx(configured['server'], REVISION, configured['web'])
    assert 'listen 127.0.0.1:9443 ssl;' not in output
    assert 'second.example.test 127.0.0.1:' not in output
    assert 'server_name second.example.test;' in output
    assert 'return 308 https://second.example.test' not in output


def test_two_argument_renderer_remains_the_legacy_default():
    server = deployment_spec(public_port=10443)
    output = render_nginx(server, REVISION)
    assert output == render_nginx(server, REVISION, normalize_web({'server': server}))
    assert 'return 308 https://site.example.test:10443$request_uri;' in output
    assert 'proxy_pass http://127.0.0.1:18890;' in output
    assert 'listen 127.0.0.1:8443 ssl;' in output
    assert 'if ($host != site.example.test) { return 444; }' in output


@pytest.mark.parametrize('identity', ['../escape', 'B' * 32, '', None])
def test_site_directory_rejects_unsafe_ids(identity):
    with pytest.raises(ValueError):
        site_directory(identity)
