"""Local node lifecycle, dependency reservation and pinned-core rendering."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from proxyhub import hy2, nodes
from proxyhub.reality import normalize_reality
from test_certificates import pairs
from test_websites import CERTIFICATE, configured


TLS = {'tls': True, 'sni': 'site.example.test', 'certificate_id': 'default'}


def create(config, protocol='vless', options=None, port=20001, name=None):
    return nodes.change(config, 'nodes.create', {
        'protocol': protocol, 'name': name if name is not None else protocol.upper() + ' example', 'port': port,
        'options': options if options is not None else TLS})


CASES = (
    [('vless', {**TLS, 'transport': transport, **({'host': 'site.example.test', 'path': '/proxy'}
                                               if transport != 'tcp' else {})})
     for transport in ('tcp', 'ws', 'h2', 'httpupgrade')]
    + [('vmess', {**TLS, 'transport': transport, **({'host': 'site.example.test', 'path': '/proxy'}
                                                  if transport != 'tcp' else {})})
       for transport in ('tcp', 'ws', 'h2', 'httpupgrade')]
    + [('vmess', {'tls': False, 'transport': 'http', 'security': 'none',
                  'host': 'site.example.test', 'path': '/proxy'})]
    + [('trojan', {**TLS, 'transport': transport, **({'host': 'site.example.test', 'path': '/proxy'}
                                                   if transport != 'tcp' else {})})
       for transport in ('tcp', 'ws', 'httpupgrade')]
    + [('ss', {'method': method}) for method in nodes.SS_METHODS]
    + [('socks5', {'auth': auth, 'udp': udp}) for auth, udp in ((True, True), (False, True), (True, False))]
    + [('tuic', {**TLS, 'congestion_control': 'bbr', 'udp_relay_mode': relay}) for relay in ('native', 'quic')]
    + [('anytls', TLS)]
    + [('vless', {'tls': True, 'tls_mode': 'reality', 'sni': 'site.example.test',
                  'reality_target_id': 'default', 'transport': transport,
                  **({'host': 'site.example.test', 'path': '/proxy'} if transport == 'h2' else {})})
       for transport in ('tcp', 'h2')]
)


def test_absence_is_compatible_and_explicit_null_is_rejected(configured):
    before = deepcopy(configured)
    assert nodes.normalize_nodes(configured) == []
    assert nodes.public_resources(configured) == {'nodes': []}
    assert configured == before
    with pytest.raises(ValueError):
        nodes.normalize_nodes({**configured, 'nodes': None})


@pytest.mark.parametrize(('protocol', 'options'), CASES)
def test_each_supported_combination_keeps_stable_lifecycle_and_secrets_private(configured, protocol, options):
    before = deepcopy(configured)
    result = create(configured, protocol, options)
    node = result['nodes'][0]
    assert len(node['id']) == 32 and node['listener_id'] == 'node-' + node['id']
    public = nodes.public_resources(result)['nodes'][0]
    assert 'credentials' not in public and all(value not in str(public) for value in node['credentials'].values())
    snapshot = nodes.client_node(result, node)
    assert snapshot['server'] == result['server']['public_address']
    assert snapshot['options'] == node['options']
    updated = nodes.change(result, 'nodes.update', {'node_id': node['id'], 'name': 'New name', 'port': 20002})
    assert updated['nodes'][0]['credentials'] == node['credentials']
    disabled = nodes.change(updated, 'nodes.disable', {'node_id': node['id']})
    assert nodes.client_node(disabled, disabled['nodes'][0]) is None
    enabled = nodes.change(disabled, 'nodes.enable', {'node_id': node['id']})
    assert enabled['nodes'][0] == updated['nodes'][0]
    with pytest.raises(ValueError, match='Disable'):
        nodes.change(enabled, 'nodes.delete', {'node_id': node['id']})
    assert nodes.change(disabled, 'nodes.delete', {'node_id': node['id']})['nodes'] == []
    assert configured == before


@pytest.mark.parametrize(('protocol', 'options'), CASES)
def test_every_rendered_combination_passes_real_pinned_singbox_check(configured, protocol, options, pairs, tmp_path):
    core = Path('.cache/runtime/windows-amd64/sing-box.exe') if os.name == 'nt' else Path('/opt/proxyhub/bin/sing-box')
    if not core.is_file():
        pytest.skip('Pinned sing-box binary is unavailable')
    config = create(configured, protocol, options)
    rendered = nodes.render_node(config, config['nodes'][0])
    tls = rendered['inbounds'][0].get('tls', {})
    if protocol == 'tuic':
        assert tls['alpn'] == ['h3']
    if 'certificate_path' in tls:
        tls.pop('certificate_path')
        tls.pop('key_path')
        tls.update(certificate=pairs[0][0].decode().splitlines(), key=pairs[0][1].decode().splitlines())
    path = tmp_path / 'node.json'
    path.write_text(json.dumps(rendered), encoding='utf-8')
    checked = subprocess.run([str(core.resolve()), 'check', '-c', str(path)], capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize(('protocol', 'options'), [
    ('trojan', {**TLS, 'transport': 'h2', 'host': 'site.example.test'}),
    ('trojan', {'tls': False}), ('tuic', {'tls': False}), ('anytls', {'tls': False}),
    ('vless', {**TLS, 'transport': 'grpc'}),
    ('vless', {**TLS, 'transport': 'tcp', 'path': '/unused'}),
    ('vless', {'tls': False, 'sni': 'site.example.test'}),
    ('vless', {'tls': False, 'flow': 'xtls-rprx-vision'}),
    ('vless', {**TLS, 'transport': 'ws', 'host': 'site.example.test', 'flow': 'xtls-rprx-vision'}),
    ('vless', {**TLS, 'transport': 'h2', 'host': 'site.example.test', 'tls': False}),
    ('vless', {**TLS, 'tls_mode': 'reality', 'reality_target_id': 'default'}),
    ('vless', {'tls': True, 'tls_mode': 'reality', 'sni': 'wrong.example.test', 'reality_target_id': 'default'}),
    ('vless', {'tls': True, 'tls_mode': 'reality', 'sni': 'site.example.test', 'reality_target_id': 'default',
               'transport': 'ws', 'host': 'site.example.test'}),
    ('vless', {**TLS, 'transport': 'ws', 'host': 'site.example.test', 'path': '/x?query=1'}),
    ('vless', {**TLS, 'sni': 'uncovered.example.test'}),
    ('vless', {**TLS, 'tls': 1}), ('vless', {**TLS, 'transport': []}),
    ('vless', {**TLS, 'flow': []}), ('vmess', {**TLS, 'security': []}),
    ('vmess', {**TLS, 'security': 'zero'}),
    ('vmess', {**TLS, 'transport': 'http', 'host': 'site.example.test', 'security': 'none'}),
    ('vmess', {'tls': False, 'transport': 'http', 'host': 'site.example.test'}),
    ('ss', {'method': 'none'}), ('ss', {'method': []}), ('ss', {'plugin': 'obfs'}),
    ('socks5', {'auth': 1}), ('socks5', {'udp': 1}), ('socks5', {'tls': True}),
    ('tuic', {**TLS, 'congestion_control': 'invalid'}), ('tuic', {**TLS, 'udp_relay_mode': 'tcp'}),
    ('anytls', {**TLS, 'transport': 'ws'}),
])
def test_unsupported_or_lossy_options_fail_closed(configured, protocol, options):
    with pytest.raises(ValueError):
        create(configured, protocol, options)


def test_reserved_ports_are_transport_specific_and_include_disabled_resources(configured):
    result = hy2.change(configured, 'hy2.create', {'server_name': 'site.example.test', 'certificate_id': 'default'})
    with pytest.raises(ValueError, match='port conflicts'):
        create(result, 'tuic', TLS, 443)
    result = create(result, 'tuic', TLS, 20001)
    result = nodes.change(result, 'nodes.disable', {'node_id': result['nodes'][0]['id']})
    with pytest.raises(ValueError, match='port conflicts'):
        create(result, 'tuic', TLS, 20001, 'Second TUIC')
    result = create(result, 'trojan', TLS, 20001)
    assert len(result['nodes']) == 2
    for port in (80, 443, 2443, 8443, 8444, 18890, 9443):
        with pytest.raises(ValueError, match='port conflicts'):
            create(configured, 'vless', TLS, port)


@pytest.mark.parametrize('name', ['PROXY', 'DIRECT', '节点选择', 'bad,name', '', '\ncontrol'])
def test_reserved_names_rejected(configured, name):
    with pytest.raises(ValueError):
        create(configured, name=name)


def test_existing_nodes_hy2_and_reality_names_are_reserved(configured):
    with pytest.raises(ValueError):
        create(configured, name=configured['server']['node_name'])
    configured = hy2.change(configured, 'hy2.create', {'server_name': 'site.example.test', 'certificate_id': 'default'})
    with pytest.raises(ValueError):
        create(configured, name=configured['hy2']['name'])
    first = create(configured)
    with pytest.raises(ValueError):
        create(first, port=20002)


def test_certificate_dependencies_include_disabled_and_indirect_reality_bindings(configured):
    config = create(configured, 'anytls')
    config = nodes.change(config, 'nodes.disable', {'node_id': config['nodes'][0]['id']})
    config = create(config, 'vless', {'tls': True, 'tls_mode': 'reality',
                    'sni': 'site.example.test', 'reality_target_id': 'default'}, 20002)
    consumers = nodes.certificate_consumers(config, 'default')
    assert {item['id'] for item in consumers} == {node['id'] for node in config['nodes']}
    assert consumers[0]['enabled'] is False and consumers[1]['enabled'] is True
    assert nodes.certificate_consumers(config, CERTIFICATE) == []
    config['web']['certificates'][0]['domains'] = ['sub.example.test']
    with pytest.raises(ValueError):
        nodes.normalize_nodes(config)


def test_credential_edits_and_incompatible_2022_method_changes_are_rejected(configured):
    config = create(configured, 'ss', {'method': '2022-blake3-aes-128-gcm'})
    node = config['nodes'][0]
    assert len(base64.b64decode(node['credentials']['password'])) == 16
    for values in ({'credentials': node['credentials']}, {'protocol': 'socks5'}, {'id': 'a' * 32}):
        with pytest.raises(ValueError):
            nodes.change(config, 'nodes.update', {'node_id': node['id'], **values})
    with pytest.raises(ValueError, match='Base64 key'):
        nodes.change(config, 'nodes.update', {'node_id': node['id'], 'options': {'method': '2022-blake3-aes-256-gcm'}})
    for password in ('invalid', base64.b64encode(b'x' * 32).decode(), 'x' * 24, 123):
        corrupt = deepcopy(config)
        corrupt['nodes'][0]['credentials']['password'] = password
        with pytest.raises(ValueError):
            nodes.normalize_nodes(corrupt)


def test_auth_toggle_keeps_retained_credential_and_disabled_udp_is_enforced(configured):
    config = create(configured, 'socks5', {'auth': False, 'udp': False})
    node = config['nodes'][0]
    rendered = nodes.render_node(config, node)
    assert 'users' not in rendered['inbounds'][0]
    assert rendered['route']['rules'] == [{'network': 'udp', 'action': 'reject'}]
    assert nodes.client_node(config, node)['credentials'] == {}
    updated = nodes.change(config, 'nodes.update', {'node_id': node['id'], 'options': {'auth': True, 'udp': True}})
    assert updated['nodes'][0]['credentials'] == node['credentials']
    assert nodes.render_node(updated, updated['nodes'][0])['inbounds'][0]['users'] == [node['credentials']]


def test_identity_bound_and_current_snapshot_validation(configured):
    config = create(configured, 'socks5', {})
    node = config['nodes'][0]
    for field, value in (('id', 'bad'), ('listener_id', 'foreign'), ('enabled', 1),
                         ('port', True), ('port', 65536), ('options', None), ('credentials', {})):
        corrupt = deepcopy(config)
        corrupt['nodes'][0][field] = value
        with pytest.raises(ValueError):
            nodes.normalize_nodes(corrupt)
    with pytest.raises(ValueError):
        nodes.normalize_nodes({**config, 'nodes': config['nodes'] * 33})
    with pytest.raises(ValueError):
        nodes.render_node(config, {**node, 'name': 'stale'})
    with pytest.raises(ValueError):
        nodes.change(config, 'nodes.enable', {'node_id': node['id']})
    with pytest.raises(ValueError):
        nodes.change(config, 'nodes.update', {'node_id': node['id']})
    with pytest.raises(ValueError):
        nodes.change(config, 'nodes.disable', {'node_id': node['id'], 'extra': 1})


def test_reality_snapshot_has_only_public_key_and_own_uuid(configured):
    config = create(configured, options={'tls_mode': 'reality', 'sni': 'site.example.test', 'reality_target_id': 'default'})
    node = config['nodes'][0]
    snapshot = nodes.client_node(config, node)
    assert snapshot['credentials'] == {'uuid': node['credentials']['uuid'],
               'public_key': config['server']['public_key'], 'short_id': config['server']['short_id']}
    assert node['credentials']['uuid'] != config['server']['uuid']
    assert 'private_key' not in str(snapshot)
    assert nodes.render_node(config, node)['inbounds'][0]['tls']['reality']['handshake'] == {
        'server': '127.0.0.1', 'server_port': config['server']['site_tls_port']}
    config['reality'] = normalize_reality(config)
    config['reality']['targets'][0]['server_name'] = 'second.example.test'
    with pytest.raises(ValueError):
        nodes.normalize_nodes(config)
