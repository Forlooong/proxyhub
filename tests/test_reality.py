"""REALITY transitions retain old entries and probe only validated TLS targets."""
from copy import deepcopy
import ipaddress
import subprocess
from types import SimpleNamespace

import pytest

from proxyhub import reality
from proxyhub.management_errors import ManagementError
from test_websites import configured, SITE


def test_legacy_resources_keep_identity_without_mutating_config(configured):
    before = deepcopy(configured)
    resources = reality.normalize_reality(configured)
    assert resources['primary_endpoint_id'] == 'default'
    assert reality.endpoint_target(configured, resources['endpoints'][0]) == {
        'server': '127.0.0.1', 'server_port': 8443,
        'server_name': 'site.example.test', 'site_id': 'default'}
    public = reality.public_resources(configured)
    assert public['node_id'] == 'local-reality'
    assert public['endpoints'][0]['listener_id'] == 'reality-default'
    assert public['endpoints'][0]['primary'] is True
    assert public['endpoints'][0]['address'] == '127.0.0.1'
    assert configured == before
    assert configured['server']['private_key'] not in str(public)


def migrated(config):
    return reality.change(config, 'reality.migrate', {'type': 'site', 'site_id': SITE,
                                                     'sni': 'second.example.test'})


def test_migration_and_explicit_selection_retirement_preserve_configuration(configured):
    before = deepcopy(configured)
    result = migrated(configured)
    resources = result['reality']
    new_id = resources['primary_endpoint_id']
    old = reality.get_endpoint(resources, 'default')
    new = reality.get_endpoint(resources, new_id)
    assert old['listen_port'] == 2443 and new['listen_port'] == 2444
    assert new['listener_id'] == 'reality-' + new_id
    assert reality.endpoint_target(result, new)['server_port'] == 9443
    assert len(resources['targets']) == len(resources['endpoints']) == 2
    assert {key: value for key, value in result.items() if key != 'reality'} == configured
    assert configured == before
    with pytest.raises(ManagementError, match='当前发布入口'):
        reality.change(result, 'reality.retire', {'endpoint_id': new_id})
    selected = reality.change(result, 'reality.select', {'endpoint_id': 'default'})
    retired = reality.change(selected, 'reality.retire', {'endpoint_id': new_id})
    assert retired['reality'] == reality.normalize_reality(configured)
    assert len(result['reality']['endpoints']) == 2
    with pytest.raises(ManagementError):
        reality.change(retired, 'reality.retire', {'endpoint_id': 'default'})


def test_default_endpoint_can_be_explicitly_retired_after_selecting_new(configured):
    result = reality.change(migrated(configured), 'reality.retire', {'endpoint_id': 'default'})
    assert len(result['reality']['endpoints']) == 1
    assert result['server'] == configured['server']
    assert result['web'] == configured['web']


@pytest.mark.parametrize('mutate', [
    lambda value: value['reality'].update(extra=True),
    lambda value: value['reality'].update(primary_endpoint_id='f' * 32),
    lambda value: value['reality']['endpoints'][1].update(listener_id='reality-default'),
    lambda value: value['reality']['endpoints'][1].update(listen_port=2443),
    lambda value: value['reality']['endpoints'][1].update(listen_port=9443),
    lambda value: value['reality']['endpoints'][1].update(listen_port=443),
    lambda value: value['reality']['endpoints'][1].update(listen_port=True),
    lambda value: value['reality']['endpoints'][1].update(id='default'),
    lambda value: value['reality']['endpoints'][1].update(target_id='default'),
    lambda value: value['reality']['targets'][1].update(id='default'),
    lambda value: value['reality']['targets'][1].update(server_name='sub.example.test'),
    lambda value: value['reality']['targets'][1].update(server_name='site.example.test'),
    lambda value: value['reality']['targets'][1].update(server_name='unbound.example.test'),
    lambda value: value['reality']['targets'][1].update(extra=True),
    lambda value: value['web']['sites'][1].update(enabled=False),
    lambda value: value['reality']['targets'].append(deepcopy(value['reality']['targets'][1])),
])
def test_conflicting_or_dangling_resources_are_rejected(configured, mutate):
    result = migrated(configured)
    mutate(result)
    with pytest.raises(ValueError):
        reality.normalize_reality(result)


def test_reuse_existing_sni_by_selection_instead_of_duplicate(configured):
    result = migrated(configured)
    with pytest.raises(ManagementError, match='已有保留入口'):
        migrated(result)


def test_port_allocation_skips_managed_website_listener(configured):
    configured['web']['sites'][1]['tls_port'] = 2444
    result = migrated(configured)
    assert result['reality']['endpoints'][1]['listen_port'] == 2445


@pytest.mark.parametrize(('protocol', 'enabled', 'expected'), [
    ('socks5', True, 2445), ('socks5', False, 2445), ('tuic', True, 2444),
])
def test_port_allocation_reserves_protocol_tcp_ports_even_when_disabled(configured, protocol, enabled, expected):
    from proxyhub import nodes
    config = nodes.change(configured, 'nodes.create', {
        'protocol': protocol, 'name': 'Retained protocol', 'port': 2444,
        'options': {} if protocol == 'socks5' else {'sni': 'site.example.test', 'certificate_id': 'default'}})
    config['nodes'][0]['enabled'] = enabled
    result = migrated(config)
    assert result['reality']['endpoints'][1]['listen_port'] == expected
    assert nodes.normalize_nodes(result) == config['nodes']


def test_retained_endpoint_limit(configured):
    result = deepcopy(configured)
    sites = result['web']['sites'][1]
    certificate = result['web']['certificates'][1]
    for index in range(7):
        name = f'alias{index}.example.test'
        sites['domains'].append(name)
        certificate['domains'].append(name)
        result = reality.change(result, 'reality.migrate', {'type': 'site', 'site_id': SITE, 'sni': name})
        sites, certificate = result['web']['sites'][1], result['web']['certificates'][1]
    with pytest.raises(ManagementError, match='8 个'):
        migrated(result)


def external(config, monkeypatch):
    monkeypatch.setattr(reality, 'resolve_external', lambda *_: '1.1.1.1')
    return reality.change(config, 'reality.migrate', {'type': 'external', 'address': 'target.example.test',
                                                     'port': 443, 'sni': 'tls.example.test'})


def test_external_address_sni_and_pinned_ip_have_independent_roles(configured, monkeypatch):
    result = external(configured, monkeypatch)
    resource = reality.public_resources(result)['endpoints'][1]
    assert resource['address'] == 'target.example.test' and resource['sni'] == 'tls.example.test'
    assert resource['site_id'] is None and resource['target_type'] == 'external'
    assert reality.endpoint_target(result, result['reality']['endpoints'][1]) == {
        'server': '1.1.1.1', 'server_port': 443, 'server_name': 'tls.example.test'}


@pytest.mark.parametrize('field,value', [
    ('port', 8443), ('port', True), ('address', '1.1.1.1'),
    ('address', 'https://example.test/'), ('server_name', 'second.example.test'),
    ('server_name', 'sub.example.test'), ('pinned_address', '127.0.0.1'),
    ('pinned_address', '10.0.0.1'), ('pinned_address', '169.254.169.254'),
    ('pinned_address', '224.0.0.1'), ('pinned_address', '::ffff:1.1.1.1'),
    ('pinned_address', '64:ff9b::a00:1'), ('pinned_address', '2002:a00:1::'),
    ('pinned_address', '2606:4700:4700:0:0:0:0:1111'),
])
def test_external_contract_rejects_unsafe_and_conflicting_values(configured, monkeypatch, field, value):
    result = external(configured, monkeypatch)
    result['reality']['targets'][1][field] = value
    with pytest.raises(ValueError):
        reality.normalize_reality(result)


def addresses(monkeypatch, target):
    monkeypatch.setattr(reality, '_addresses', lambda host: {
        ipaddress.ip_address(value) for value in (target if host == 'target.example.test' else ['8.8.8.8'])})
    monkeypatch.setattr(reality, '_local_addresses', lambda: {ipaddress.ip_address('9.9.9.9')})


@pytest.mark.parametrize('target', [
    ['1.1.1.1', '10.0.0.1'], ['1.1.1.1', 'fe80::1'], ['8.8.8.8'], ['9.9.9.9'], [],
    ['1.1.1.1', '::ffff:1.1.1.1'], ['1.1.1.1', '224.0.0.1'],
])
def test_resolver_rejects_every_unsafe_record_and_self_address(configured, monkeypatch, target):
    addresses(monkeypatch, target)
    with pytest.raises(ManagementError, match='全部 A / AAAA'):
        reality.resolve_external(configured['server'], 'target.example.test')


def test_resolver_pins_a_deterministic_address_after_checking_all_records(configured, monkeypatch):
    addresses(monkeypatch, ['2606:4700:4700::1111', '1.1.1.1'])
    assert reality.resolve_external(configured['server'], 'target.example.test') == '1.1.1.1'


def test_dns_child_has_timeout_and_bounded_results(monkeypatch):
    calls = []
    def lookup(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='["1.1.1.1"]')
    monkeypatch.setattr(reality.subprocess, 'run', lookup)
    assert reality._addresses('target.example.test') == {ipaddress.ip_address('1.1.1.1')}
    assert calls[0][1]['timeout'] == 5
    assert calls[0][0][-1] == 'target.example.test'
    monkeypatch.setattr(reality.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='x' * 8193))
    with pytest.raises(ValueError):
        reality._addresses('target.example.test')


def test_probe_uses_only_trusted_tls_and_fixed_addresses(configured, monkeypatch):
    result = external(configured, monkeypatch)
    addresses(monkeypatch, ['1.1.1.1'])
    calls = []
    def check(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(reality.subprocess, 'run', check)
    reality.probe_targets(result)
    assert len(calls) == 2
    assert '127.0.0.1:8443' in calls[0][0]
    assert '1.1.1.1:443' in calls[1][0]
    assert 'target.example.test' not in calls[1][0]
    for argv, options in calls:
        assert '-tls1_3' in argv and argv[argv.index('-groups') + 1] == 'X25519'
        assert '-verify_return_error' in argv and '-verify_hostname' in argv
        assert options['stdout'] == options['stderr'] == subprocess.DEVNULL
        assert options['input'] == '' and options['timeout'] == 6


def test_probe_rejects_rebinding_before_dialing_external_address(configured, monkeypatch):
    result = external(configured, monkeypatch)
    addresses(monkeypatch, ['1.0.0.1'])
    calls = []
    monkeypatch.setattr(reality.subprocess, 'run', lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0))
    with pytest.raises(ManagementError, match='固定地址'):
        reality.probe_targets(result)
    assert len(calls) == 1 and '127.0.0.1:8443' in calls[0]


@pytest.mark.parametrize('failure', [
    SimpleNamespace(returncode=1), OSError('private upstream detail'),
    subprocess.TimeoutExpired(['secret target'], 6),
])
def test_probe_failures_are_bounded_and_redacted(configured, monkeypatch, failure):
    def check(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(reality.subprocess, 'run', check)
    with pytest.raises(ManagementError) as caught:
        reality.probe_targets(configured)
    assert caught.value.code == 'TARGET_CHECK_FAILED'
    assert 'private' not in str(caught.value) and 'secret' not in str(caught.value)
