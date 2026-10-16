"""Local protocol resources and their fixed shared sing-box runtime."""
from __future__ import annotations

import base64
from collections.abc import Mapping
from copy import deepcopy
import re
import secrets
import uuid

from .proxy import _text, validate_domain, validate_spec


KINDS = frozenset('nodes.' + value for value in ('create', 'update', 'enable', 'disable', 'delete'))
PROTOCOLS = ('vless', 'vmess', 'trojan', 'ss', 'socks5', 'tuic', 'anytls')
SS_METHODS = ('aes-128-gcm', 'aes-256-gcm', 'chacha20-ietf-poly1305',
              '2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm',
              '2022-blake3-chacha20-poly1305')
_FIELDS = {'id', 'listener_id', 'protocol', 'enabled', 'name', 'port', 'options', 'credentials'}
_EDITABLE = {'name', 'port', 'options'}
_RESERVED_NAMES = {'PROXY', 'DIRECT', 'REJECT', 'GLOBAL', 'PASS', 'REJECT-DROP',
                   'COMPATIBLE', '节点选择'}
_ID = re.compile(r'[0-9a-f]{32}\Z')
_TOKEN = re.compile(r'[A-Za-z0-9_-]{32,128}\Z')
_UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')


def _shape(value, fields):
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError('Node fields are missing or unsupported')


def _boolean(value, field):
    if type(value) is not bool:
        raise ValueError(field + ' must be a boolean')
    return value


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('Node ID must be 32 lowercase hexadecimal digits')
    return value


def get_node(nodes: list, identity: str) -> dict:
    _identity(identity)
    for node in nodes:
        if node['id'] == identity:
            return node
    raise ValueError('Unknown node ID')


def listener_networks(node: Mapping) -> tuple[str, ...]:
    """Reserve both transports for SS and SOCKS, including disabled resources."""
    if node['protocol'] == 'tuic':
        return ('udp',)
    if node['protocol'] in {'ss', 'socks5'}:
        return ('tcp', 'udp')
    return ('tcp',)


def _tls_options(config, protocol, options):
    from .websites import get_certificate, normalize_web
    tls = _boolean(options.get('tls', True), 'TLS')
    if protocol in {'trojan', 'tuic', 'anytls'} and not tls:
        raise ValueError('This protocol requires trusted certificate TLS')
    result = {'tls': tls}
    if not tls:
        if set(options) & {'tls_mode', 'sni', 'certificate_id', 'reality_target_id'}:
            raise ValueError('TLS fields cannot be used when TLS is disabled')
        return result
    mode = options.get('tls_mode', 'certificate')
    result.update(tls_mode=mode, sni=validate_domain(options.get('sni'), 'SNI'))
    if mode == 'certificate':
        if 'reality_target_id' in options:
            raise ValueError('Certificate TLS cannot reference a REALITY target')
        certificate = get_certificate(normalize_web(config), options.get('certificate_id'))
        if result['sni'] not in certificate['domains']:
            raise ValueError('TLS SNI must be covered by the selected managed certificate')
        result['certificate_id'] = certificate['id']
    elif mode == 'reality' and protocol == 'vless':
        from .reality import get_target, normalize_reality
        if 'certificate_id' in options:
            raise ValueError('REALITY cannot use a certificate binding')
        target = get_target(normalize_reality(config), options.get('reality_target_id'))
        if result['sni'] != target['server_name']:
            raise ValueError('REALITY SNI must match the selected target')
        result['reality_target_id'] = target['id']
    else:
        raise ValueError('Unsupported TLS mode')
    return result


def _options(config, protocol, value):
    if not isinstance(value, Mapping):
        raise ValueError('Node options must be an object')
    allowed = set()
    result = {}
    if protocol in {'vless', 'vmess', 'trojan', 'tuic', 'anytls'}:
        allowed |= {'tls', 'tls_mode', 'sni', 'certificate_id', 'reality_target_id'}
        result.update(_tls_options(config, protocol, value))
    if protocol in {'vless', 'vmess', 'trojan'}:
        allowed |= {'transport', 'host', 'path'}
        transport = value.get('transport', 'tcp')
        supported = {'tcp', 'ws', 'httpupgrade'}
        if protocol in {'vless', 'vmess'}:
            supported.add('h2')
        if protocol == 'vmess':
            supported.add('http')
        if not isinstance(transport, str) or transport not in supported:
            raise ValueError('Transport is unsupported by the pinned server/client pair')
        result['transport'] = transport
        if transport == 'tcp':
            if set(value) & {'host', 'path'}:
                raise ValueError('TCP transport cannot contain HTTP fields')
        else:
            result['host'] = validate_domain(value.get('host'), 'Transport host')
            path = value.get('path', '/')
            if (not isinstance(path, str) or not path.startswith('/') or len(path) > 256
                    or any(ord(char) < 33 or ord(char) > 126 for char in path)
                    or any(char in path for char in '?#')):
                raise ValueError('Transport path must be an ASCII path without query or fragment')
            result['path'] = path
        if transport == 'h2' and not result['tls']:
            raise ValueError('H2 transport requires TLS')
        if result.get('tls_mode') == 'reality' and transport not in {'tcp', 'h2'}:
            raise ValueError('REALITY is supported only with TCP or H2')
    if protocol == 'vless':
        allowed.add('flow')
        flow = value.get('flow', '')
        if not isinstance(flow, str) or flow not in {'', 'xtls-rprx-vision'} or (flow and (result['transport'] != 'tcp' or not result['tls'])):
            raise ValueError('Vision requires VLESS TCP with TLS')
        result['flow'] = flow
    if protocol == 'vmess':
        allowed.add('security')
        security = value.get('security', 'auto')
        if not isinstance(security, str) or security not in {'auto', 'none', 'aes-128-gcm', 'chacha20-poly1305'}:
            raise ValueError('Unsupported VMess AEAD security')
        if result['transport'] == 'http' and (result['tls'] or security != 'none'):
            raise ValueError('VMess HTTP/1 requires TLS disabled and security none')
        result['security'] = security
    if protocol == 'ss':
        allowed.add('method')
        method = value.get('method', 'aes-256-gcm')
        if not isinstance(method, str) or method not in SS_METHODS:
            raise ValueError('Unsupported Shadowsocks AEAD method')
        result['method'] = method
    if protocol == 'socks5':
        allowed |= {'auth', 'udp'}
        result.update(auth=_boolean(value.get('auth', True), 'SOCKS authentication'),
                      udp=_boolean(value.get('udp', True), 'SOCKS UDP'))
    if protocol == 'tuic':
        allowed |= {'congestion_control', 'udp_relay_mode'}
        congestion, relay = value.get('congestion_control', 'bbr'), value.get('udp_relay_mode', 'native')
        if (not isinstance(congestion, str) or not isinstance(relay, str)
                or congestion not in {'bbr', 'cubic', 'new_reno'} or relay not in {'native', 'quic'}):
            raise ValueError('Unsupported TUIC v5 congestion or UDP relay mode')
        result.update(congestion_control=congestion, udp_relay_mode=relay)
    if set(value) - allowed:
        raise ValueError('Node options contain unsupported fields')
    return result


def _credentials(protocol, options, value):
    fields = ({'uuid'} if protocol in {'vless', 'vmess'} else
              {'uuid', 'password'} if protocol == 'tuic' else
              {'username', 'password'} if protocol == 'socks5' else {'password'})
    _shape(value, fields)
    result = dict(value)
    if 'uuid' in result:
        raw = result['uuid']
        if not isinstance(raw, str) or not _UUID.fullmatch(raw) or str(uuid.UUID(raw)) != raw:
            raise ValueError('Node UUID must use canonical lowercase UUID format')
    if 'username' in result and (not isinstance(result['username'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', result['username'])):
        raise ValueError('SOCKS username must contain 8–64 URL-safe characters')
    if 'password' in result:
        password = result['password']
        if protocol == 'ss' and options['method'].startswith('2022-'):
            try:
                decoded = base64.b64decode(password, validate=True)
                size = 16 if options['method'] == '2022-blake3-aes-128-gcm' else 32
                valid = len(decoded) == size and base64.b64encode(decoded).decode('ascii') == password
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise ValueError('Shadowsocks 2022 requires a canonical Base64 key of the method size')
        elif not isinstance(password, str) or not _TOKEN.fullmatch(password):
            raise ValueError('Node password must contain 32–128 URL-safe characters')
    return result


def normalize_nodes(config: Mapping) -> list[dict]:
    """Return validated resources; retained disabled nodes keep their dependencies."""
    from .websites import normalize_web
    from .reality import normalize_reality
    if not isinstance(config, Mapping):
        raise ValueError('Nodes require a configuration object')
    values = config.get('nodes', [])
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError('Provide at most 32 independent protocol nodes')
    if not values:
        return []
    server = validate_spec(config['server'])
    names = _RESERVED_NAMES | {server['node_name']}
    if config.get('hy2') is not None:
        names.add(config['hy2']['name'])
    ports = {('tcp', server[field]) for field in ('http_port', 'public_port', 'reality_port',
                    'site_tls_port', 'subscription_tls_port', 'publisher_port')}
    ports |= {('tcp', site['tls_port']) for site in normalize_web(config)['sites']}
    ports |= {('tcp', endpoint['listen_port']) for endpoint in normalize_reality(config)['endpoints']}
    if config.get('hy2') is not None:
        ports.add(('udp', config['hy2']['port']))
    result, identities = [], set()
    for value in values:
        _shape(value, _FIELDS)
        node = deepcopy(value)
        identity = _identity(node['id'])
        if identity in identities or node['listener_id'] != 'node-' + identity:
            raise ValueError('Node listener ID must match its unique stable identity')
        identities.add(identity)
        protocol = node['protocol']
        if not isinstance(protocol, str) or protocol not in PROTOCOLS:
            raise ValueError('Unsupported local node protocol')
        _boolean(node['enabled'], 'Node enabled')
        name = node['name'] = _text(node['name'], 'Node name')
        if name in names or ',' in name:
            raise ValueError('Node name conflicts with another node or reserved group')
        names.add(name)
        port = node['port']
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Node port must be an integer between 1 and 65535')
        for network in listener_networks(node):
            if (network, port) in ports:
                raise ValueError('Node port conflicts with a reserved managed listener')
            ports.add((network, port))
        node['options'] = _options(config, protocol, node['options'])
        node['credentials'] = _credentials(protocol, node['options'], node['credentials'])
        result.append(node)
    return result


def _new_credentials(protocol, options):
    value = {}
    if protocol in {'vless', 'vmess', 'tuic'}:
        value['uuid'] = str(uuid.uuid4())
    if protocol not in {'vless', 'vmess'}:
        value['password'] = secrets.token_urlsafe(32)
        if protocol == 'ss' and options.get('method', '').startswith('2022-'):
            size = 16 if options['method'] == '2022-blake3-aes-128-gcm' else 32
            value['password'] = base64.b64encode(secrets.token_bytes(size)).decode('ascii')
    if protocol == 'socks5':
        value['username'] = secrets.token_urlsafe(12)
    return value


def change(config: Mapping, kind: str, values: Mapping) -> dict:
    if not isinstance(values, Mapping):
        raise ValueError('Node operation values must be an object')
    before = normalize_nodes(config)
    result = deepcopy(config)
    nodes = result['nodes'] = deepcopy(before)
    if kind == 'nodes.create':
        if set(values) - {'protocol', 'name', 'port', 'options'} or not {'protocol', 'name', 'port'} <= set(values):
            raise ValueError('Node create fields are missing or unsupported')
        protocol = values['protocol']
        if not isinstance(protocol, str) or protocol not in PROTOCOLS:
            raise ValueError('Unsupported local node protocol')
        options = _options(config, protocol, values.get('options', {}))
        identity = uuid.uuid4().hex
        nodes.append({'id': identity, 'listener_id': 'node-' + identity, 'enabled': True,
                      'protocol': protocol, 'name': values['name'], 'port': values['port'],
                      'options': options, 'credentials': _new_credentials(protocol, options)})
    elif kind in KINDS:
        if 'node_id' not in values:
            raise ValueError('Node operation requires node_id')
        node = get_node(nodes, values['node_id'])
        if kind == 'nodes.update':
            if not set(values) - {'node_id'} or set(values) - (_EDITABLE | {'node_id'}):
                raise ValueError('Node update requires supported editable fields')
            node.update({key: value for key, value in values.items() if key != 'node_id'})
        else:
            if set(values) != {'node_id'}:
                raise ValueError('Node lifecycle operation accepts only node_id')
            if kind == 'nodes.delete':
                if node['enabled']:
                    raise ValueError('Disable a node before deleting it')
                nodes.remove(node)
            else:
                node['enabled'] = kind == 'nodes.enable'
    else:
        raise ValueError('Unsupported node operation')
    result['nodes'] = normalize_nodes(result)
    if result['nodes'] == before:
        raise ValueError('Node operation does not change a resource')
    return result


def public_resources(config: Mapping) -> dict:
    return {'nodes': [{key: value for key, value in node.items() if key != 'credentials'}
                      for node in normalize_nodes(config)]}


def _node(config, node):
    selected = get_node(normalize_nodes(config), node['id'])
    if selected != node:
        raise ValueError('Node renderer requires the current normalized resource')
    return selected


def render_node(config: Mapping, node: Mapping) -> dict:
    """Render one IPv4 listener, including a disabled retained resource."""
    from .websites import get_certificate, get_site, normalize_web
    node = _node(config, node)
    protocol, options, credentials = node['protocol'], node['options'], node['credentials']
    inbound = {'type': {'ss': 'shadowsocks', 'socks5': 'socks'}.get(protocol, protocol),
               'tag': node['listener_id'], 'listen': '0.0.0.0', 'listen_port': node['port']}
    if protocol == 'ss':
        inbound.update(method=options['method'], password=credentials['password'])
    elif protocol == 'socks5':
        if options['auth']:
            inbound['users'] = [dict(credentials)]
    else:
        user = {'name': node['name'], **credentials}
        if protocol == 'vless':
            user['flow'] = options['flow']
        if protocol == 'vmess':
            user['alterId'] = 0
        inbound['users'] = [user]
    if options.get('tls'):
        tls = inbound['tls'] = {'enabled': True, 'server_name': options['sni']}
        if options['tls_mode'] == 'certificate':
            certificate = get_certificate(normalize_web(config), options['certificate_id'])
            tls.update(certificate_path=certificate['certificate_path'], key_path=certificate['key_path'])
        else:
            from .reality import get_target, normalize_reality
            target = get_target(normalize_reality(config), options['reality_target_id'])
            if target['type'] == 'site':
                address, port = '127.0.0.1', get_site(normalize_web(config), target['site_id'])['tls_port']
            else:
                address, port = target['pinned_address'], target['port']
            tls['reality'] = {'enabled': True, 'handshake': {'server': address, 'server_port': port},
                              'private_key': config['server']['private_key'],
                              'short_id': [config['server']['short_id']]}
    transport = options.get('transport', 'tcp')
    if transport != 'tcp':
        transport_options = {'type': 'http' if transport in {'http', 'h2'} else transport,
                             'path': options['path']}
        if transport in {'http', 'h2'}:
            transport_options['host'] = [options['host']]
        elif transport == 'ws':
            transport_options['headers'] = {'Host': options['host']}
        else:
            transport_options['host'] = options['host']
        inbound['transport'] = transport_options
    if protocol == 'tuic':
        inbound['congestion_control'] = options['congestion_control']
        inbound['tls']['alpn'] = ['h3']
    result = {'log': {'level': 'warn', 'timestamp': True}, 'inbounds': [inbound],
              'outbounds': [{'type': 'direct', 'tag': 'direct'}]}
    if protocol == 'socks5' and not options['udp']:
        result['route'] = {'rules': [{'inbound': [node['listener_id']],
                                      'network': 'udp', 'action': 'reject'}]}
    return result


def render_shared(config: Mapping) -> dict | None:
    """Reuse the individual renderers for one enabled HY2/additional-node group."""
    from .hy2 import normalize_hy2, render_hy2
    resources = []
    hysteria = normalize_hy2(config)
    if hysteria is not None and hysteria['enabled']:
        resources.append(render_hy2(config))
    resources.extend(render_node(config, node) for node in normalize_nodes(config) if node['enabled'])
    if not resources:
        return None
    rendered = {'log': {'level': 'warn', 'timestamp': True},
                'inbounds': [inbound for resource in resources for inbound in resource['inbounds']],
                'outbounds': [{'type': 'direct', 'tag': 'direct'}]}
    for inbound in rendered['inbounds']:
        for user in inbound.get('users', []):
            if 'name' in user:
                user['name'] = inbound['tag']
    rules = [rule for resource in resources for rule in resource.get('route', {}).get('rules', [])]
    if rules:
        rendered['route'] = {'rules': rules}
    return rendered


def client_node(config: Mapping, node: Mapping) -> dict | None:
    node = _node(config, node)
    if not node['enabled']:
        return None
    options, credentials = deepcopy(node['options']), deepcopy(node['credentials'])
    if node['protocol'] == 'socks5' and not options['auth']:
        credentials = {}
    if options.get('tls_mode') == 'reality':
        credentials.update(public_key=config['server']['public_key'], short_id=config['server']['short_id'])
    return {key: node[key] for key in ('id', 'protocol', 'name', 'port')} | {
        'server': validate_spec(config['server'])['public_address'],
        'options': options, 'credentials': credentials}


def certificate_consumers(config: Mapping, certificate_id: str) -> list[dict]:
    from .websites import consumers, normalize_web
    return [item for item in consumers(normalize_web(config), config['server'], certificate_id,
            config.get('reality'), config.get('hy2'), normalize_nodes(config)) if item['kind'] == 'node']
