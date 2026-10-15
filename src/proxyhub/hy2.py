"""One managed Hysteria2 resource; credentials never enter resource listings."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re
import secrets

from .proxy import _text, validate_domain, validate_spec
from .websites import get_certificate, normalize_web


KINDS = frozenset({'hy2.create', 'hy2.update', 'hy2.enable', 'hy2.disable'})
_FIELDS = {'id', 'listener_id', 'enabled', 'name', 'port', 'server_name',
           'certificate_id', 'password'}
_EDITABLE = {'name', 'port', 'server_name', 'certificate_id'}
_RESERVED_NAMES = {'PROXY', 'DIRECT', 'REJECT', 'GLOBAL', 'PASS', 'REJECT-DROP', 'COMPATIBLE'}


def normalize_hy2(config: Mapping) -> dict | None:
    """Validate the optional resource without inspecting files or running services."""
    if not isinstance(config, Mapping):
        raise ValueError('HY2 requires a configuration object')
    if 'hy2' not in config:
        return None
    node = deepcopy(config['hy2'])
    if not isinstance(node, Mapping) or set(node) != _FIELDS:
        raise ValueError('HY2 fields are missing or unsupported')
    if node['id'] != 'local-hy2' or node['listener_id'] != 'hy2-default':
        raise ValueError('HY2 resource and listener IDs are fixed')
    if type(node['enabled']) is not bool:
        raise ValueError('HY2 enabled must be a boolean')
    node['name'] = _text(node['name'], 'HY2 name')
    server = validate_spec(config['server'])
    if (node['name'] in _RESERVED_NAMES or ',' in node['name']
            or node['name'] == server['node_name']):
        raise ValueError('HY2 name conflicts with a reserved group or REALITY node name')
    if type(node['port']) is not int or not 1 <= node['port'] <= 65535:
        raise ValueError('HY2 UDP port must be an integer between 1 and 65535')
    node['server_name'] = validate_domain(node['server_name'], 'HY2 server_name')
    get_certificate(normalize_web(config), node['certificate_id'])
    if not isinstance(node['password'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', node['password']):
        raise ValueError('HY2 password must contain 32–128 URL-safe characters')
    return node


def change(config: Mapping, kind: str, values: Mapping) -> dict:
    """Prepare a typed lifecycle change while preserving its stable credential."""
    if not isinstance(values, Mapping):
        raise ValueError('HY2 values must be an object')
    node = normalize_hy2(config)
    result = deepcopy(config)
    if kind == 'hy2.create':
        if node is not None:
            raise ValueError('HY2 resource already exists')
        if set(values) - _EDITABLE or not {'server_name', 'certificate_id'} <= set(values):
            raise ValueError('HY2 create fields are missing or unsupported')
        result['hy2'] = {'id': 'local-hy2', 'listener_id': 'hy2-default', 'enabled': True,
                         'name': 'ProxyHub HY2', 'port': 443, **values,
                         'password': secrets.token_urlsafe(32)}
    elif kind == 'hy2.update':
        if node is None:
            raise ValueError('HY2 resource does not exist')
        if not values or set(values) - _EDITABLE:
            raise ValueError('HY2 update requires supported editable fields')
        result['hy2'] = {**node, **values}
    elif kind in {'hy2.enable', 'hy2.disable'}:
        if node is None:
            raise ValueError('HY2 resource does not exist')
        if values:
            raise ValueError('HY2 enable and disable do not accept fields')
        result['hy2'] = {**node, 'enabled': kind == 'hy2.enable'}
    else:
        raise ValueError('Unsupported HY2 operation')
    result['hy2'] = normalize_hy2(result)
    if result['hy2'] == node:
        raise ValueError('HY2 operation does not change the resource')
    return result


def public_resources(config: Mapping) -> dict:
    node = normalize_hy2(config)
    return {'node': None if node is None else {key: value for key, value in node.items() if key != 'password'}}


def render_hy2(config: Mapping) -> dict | None:
    """Render a pinned-core configuration, including disabled retained resources."""
    node = normalize_hy2(config)
    if node is None:
        return None
    certificate = get_certificate(normalize_web(config), node['certificate_id'])
    return {
        'log': {'level': 'warn', 'timestamp': True},
        'inbounds': [{'type': 'hysteria2', 'tag': node['listener_id'],
                      'listen': '0.0.0.0', 'listen_port': node['port'],
                      'users': [{'name': node['name'], 'password': node['password']}],
                      'tls': {'enabled': True, 'server_name': node['server_name'],
                              'certificate_path': certificate['certificate_path'],
                              'key_path': certificate['key_path']}}],
        'outbounds': [{'type': 'direct', 'tag': 'direct'}],
    }


def client_node(config: Mapping) -> dict | None:
    """Return the sole converter input snapshot only while HY2 is enabled."""
    node = normalize_hy2(config)
    if node is None or not node['enabled']:
        return None
    return {key: node[key] for key in ('id', 'name', 'port', 'server_name', 'password')} | {
        'server': validate_spec(config['server'])['public_address']}
