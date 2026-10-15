"""HY2 lifecycle keeps one stable credential and its explicit TLS binding."""
from copy import deepcopy

import pytest

from proxyhub import hy2
from test_websites import CERTIFICATE, configured


def created(config):
    return hy2.change(config, 'hy2.create', {
        'server_name': 'future.example.test', 'certificate_id': CERTIFICATE})


def test_absent_resource_preserves_legacy_config(configured):
    before = deepcopy(configured)
    assert hy2.normalize_hy2(configured) is None
    assert hy2.public_resources(configured) == {'node': None}
    assert hy2.render_hy2(configured) is None
    assert hy2.client_node(configured) is None
    assert configured == before


def test_lifecycle_retains_password_and_disabled_certificate_binding(configured):
    before = deepcopy(configured)
    result = created(configured)
    node = result['hy2']
    assert node['id'] == 'local-hy2' and node['listener_id'] == 'hy2-default'
    assert node['name'] == 'ProxyHub HY2' and node['port'] == 443 and node['enabled'] is True
    assert len(node['password']) == 43
    public = hy2.public_resources(result)
    assert public['node'] == {key: value for key, value in node.items() if key != 'password'}
    assert node['password'] not in str(public)
    updated = hy2.change(result, 'hy2.update', {'name': 'Updated HY2', 'port': 444})
    disabled = hy2.change(updated, 'hy2.disable', {})
    assert hy2.client_node(disabled) is None
    assert disabled['hy2']['certificate_id'] == CERTIFICATE
    enabled = hy2.change(disabled, 'hy2.enable', {})
    assert enabled['hy2']['password'] == node['password']
    assert enabled['hy2']['id'] == node['id']
    assert {key: value for key, value in enabled.items() if key != 'hy2'} == before
    assert configured == before


def test_renderer_and_client_share_certificate_sni_port_and_credential(configured):
    configured['web']['sites'][1]['enabled'] = False
    result = created(configured)
    node = result['hy2']
    rendered = hy2.render_hy2(result)
    inbound = rendered['inbounds'][0]
    assert inbound == {
        'type': 'hysteria2', 'tag': 'hy2-default', 'listen': '0.0.0.0', 'listen_port': 443,
        'users': [{'name': node['name'], 'password': node['password']}],
        'tls': {'enabled': True, 'server_name': 'future.example.test',
                'certificate_path': configured['web']['certificates'][1]['certificate_path'],
                'key_path': configured['web']['certificates'][1]['key_path']}}
    assert rendered['outbounds'] == [{'type': 'direct', 'tag': 'direct'}]
    assert hy2.client_node(result) == {
        'id': 'local-hy2', 'name': node['name'], 'server': result['server']['public_address'],
        'port': 443, 'server_name': node['server_name'], 'password': node['password']}
    assert hy2.render_hy2(hy2.change(result, 'hy2.disable', {})) == rendered


@pytest.mark.parametrize(('field', 'value'), [
    ('id', 'another'), ('listener_id', 'another'), ('enabled', 1), ('port', True), ('port', 0),
    ('port', 65536), ('name', 'PROXY'), ('name', 'node,split'), ('name', 'Test proxy'),
    ('server_name', '127.0.0.1'), ('server_name', '*.example.test'),
    ('certificate_id', 'f' * 32), ('password', 'short'), ('password', 'x' * 129),
    ('password', 'x' * 31 + ':'), ('extra', 'unsupported'),
])
def test_invalid_resources_fail_closed(configured, field, value):
    result = created(configured)
    if field == 'name' and value == 'Test proxy':
        value = configured['server']['node_name']
    result['hy2'][field] = value
    with pytest.raises(ValueError):
        hy2.normalize_hy2(result)


@pytest.mark.parametrize(('kind', 'values'), [
    ('hy2.create', {'server_name': 'future.example.test', 'certificate_id': CERTIFICATE}),
    ('hy2.update', {}), ('hy2.update', {'port': 443}), ('hy2.update', {'password': 'x' * 43}),
    ('hy2.enable', {}), ('hy2.disable', {'enabled': False}), ('hy2.delete', {}),
])
def test_unknown_duplicate_and_noop_operations_rejected(configured, kind, values):
    with pytest.raises(ValueError):
        hy2.change(created(configured), kind, values)


def test_missing_resource_cannot_update_or_enable_and_null_is_not_absence(configured):
    for kind in ('hy2.update', 'hy2.enable', 'hy2.disable'):
        with pytest.raises(ValueError):
            hy2.change(configured, kind, {'name': 'new'} if kind == 'hy2.update' else {})
    with pytest.raises(ValueError):
        hy2.normalize_hy2({**configured, 'hy2': None})
