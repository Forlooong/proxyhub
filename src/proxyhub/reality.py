"""REALITY targets and independent listeners, with bounded TLS-only checks."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import ipaddress
import json
import re
import subprocess
import sys
import uuid

from .management_errors import ManagementError
from .proxy import validate_domain, validate_spec
from .websites import get_site, normalize_web


KINDS = frozenset({'reality.migrate', 'reality.select', 'reality.retire'})
_ID = re.compile(r'(?:default|[0-9a-f]{32})\Z')
_PORT_FIELDS = ('http_port', 'public_port', 'reality_port', 'site_tls_port',
                'subscription_tls_port', 'publisher_port')
_DNS_SCRIPT = '''import json, socket, sys
addresses = sorted({item[4][0] for item in socket.getaddrinfo(sys.argv[1], None, socket.AF_UNSPEC, socket.SOCK_STREAM) if item[0] in (socket.AF_INET, socket.AF_INET6)})
if not addresses or len(addresses) > 64: raise ValueError("DNS result count")
print(json.dumps(addresses))
'''


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('REALITY resource ID must be default or 32 lowercase hexadecimal digits')
    return value


def _shape(value, fields):
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError('REALITY fields are missing or unsupported')


def get_target(reality: dict, identity: str) -> dict:
    _identity(identity)
    for target in reality['targets']:
        if target['id'] == identity:
            return target
    raise ValueError('Unknown REALITY target ID')


def get_endpoint(reality: dict, identity: str) -> dict:
    _identity(identity)
    for endpoint in reality['endpoints']:
        if endpoint['id'] == identity:
            return endpoint
    raise ValueError('Unknown REALITY endpoint ID')


def _safe_address(value):
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_multicast or address.is_reserved
            or (address.version == 6 and (address.ipv4_mapped is not None
                or address.sixtofour is not None or address.teredo is not None
                or address in ipaddress.ip_network('64:ff9b::/96')))):
        raise ValueError('External REALITY target must use an ordinary global unicast address')
    return address


def _reserved_ports(server, web):
    return {server[field] for field in _PORT_FIELDS} | {site['tls_port'] for site in web['sites']}


def normalize_reality(config: Mapping) -> dict:
    """Return independent validated resources, deriving the original entry if absent."""
    if not isinstance(config, Mapping) or 'server' not in config:
        raise ValueError('REALITY resources require a server configuration')
    server, web = validate_spec(config['server']), normalize_web(config)
    if 'reality' not in config:
        return {'targets': [{'id': 'default', 'type': 'site', 'site_id': 'default',
                             'server_name': server['site_domain']}],
                'endpoints': [{'id': 'default', 'listener_id': 'reality-default',
                               'listen_port': server['reality_port'], 'target_id': 'default'}],
                'primary_endpoint_id': 'default'}
    reality = deepcopy(config['reality'])
    _shape(reality, {'targets', 'endpoints', 'primary_endpoint_id'})
    if (not isinstance(reality['endpoints'], list) or not 1 <= len(reality['endpoints']) <= 8
            or not isinstance(reality['targets'], list)
            or len(reality['targets']) != len(reality['endpoints'])):
        raise ValueError('Provide 1–8 independent REALITY endpoints and their targets')
    target_ids, names = set(), {server['subscription_domain']}
    website_domains = {domain for site in web['sites'] for domain in site['domains']}
    for target in reality['targets']:
        if not isinstance(target, Mapping):
            raise ValueError('REALITY target must be an object')
        target_type = target.get('type')
        fields = ({'id', 'type', 'site_id', 'server_name'} if target_type == 'site' else
                  {'id', 'type', 'address', 'port', 'server_name', 'pinned_address'})
        _shape(target, fields)
        identity = _identity(target['id'])
        if identity in target_ids:
            raise ValueError('Duplicate REALITY target ID')
        target_ids.add(identity)
        name = target['server_name'] = validate_domain(target['server_name'])
        if name in names:
            raise ValueError('REALITY SNI conflicts with another endpoint or subscription')
        names.add(name)
        if target_type == 'site':
            site = get_site(web, target['site_id'])
            if not site['enabled'] or name not in site['domains']:
                raise ValueError('REALITY target requires an enabled website and one of its bound domains')
        elif target_type == 'external':
            target['address'] = validate_domain(target['address'])
            if type(target['port']) is not int or target['port'] != 443:
                raise ValueError('External REALITY targets currently require HTTPS port 443')
            if name in website_domains:
                raise ValueError('External REALITY SNI conflicts with a managed website')
            value = target['pinned_address']
            if not isinstance(value, str) or str(_safe_address(value)) != value:
                raise ValueError('External REALITY target address must be canonical')
        else:
            raise ValueError('Unsupported REALITY target type')
    endpoint_ids, ports, referenced = set(), set(), set()
    reserved = _reserved_ports(server, web)
    for endpoint in reality['endpoints']:
        _shape(endpoint, {'id', 'listener_id', 'listen_port', 'target_id'})
        identity = _identity(endpoint['id'])
        if identity in endpoint_ids or endpoint['listener_id'] != 'reality-' + identity:
            raise ValueError('REALITY listener ID must match its unique stable endpoint ID')
        endpoint_ids.add(identity)
        port = endpoint['listen_port']
        if (type(port) is not int or not 1 <= port <= 65535 or port in ports
                or (port in reserved and not (identity == 'default' and port == server['reality_port']))
                or (identity == 'default' and port != server['reality_port'])):
            raise ValueError('REALITY listener port is invalid or conflicts with a managed listener')
        ports.add(port)
        target = get_target(reality, endpoint['target_id'])
        if target['id'] in referenced:
            raise ValueError('REALITY endpoints must have independent targets')
        referenced.add(target['id'])
    if referenced != target_ids:
        raise ValueError('REALITY contains an unreferenced target')
    get_endpoint(reality, reality['primary_endpoint_id'])
    return reality


def endpoint_target(config: Mapping, endpoint: Mapping) -> dict:
    reality = normalize_reality(config)
    selected = get_endpoint(reality, endpoint['id'])
    target = get_target(reality, selected['target_id'])
    if target['type'] == 'site':
        site = get_site(normalize_web(config), target['site_id'])
        return {'server': '127.0.0.1', 'server_port': site['tls_port'],
                'server_name': target['server_name'], 'site_id': site['id']}
    return {'server': target['pinned_address'], 'server_port': target['port'],
            'server_name': target['server_name']}


def public_resources(config: Mapping) -> dict:
    reality = normalize_reality(config)
    result = {'node_id': 'local-reality', 'primary_endpoint_id': reality['primary_endpoint_id'],
              'endpoints': []}
    for endpoint in reality['endpoints']:
        target = get_target(reality, endpoint['target_id'])
        network = endpoint_target(config, endpoint)
        result['endpoints'].append({**endpoint, 'sni': target['server_name'],
            'primary': endpoint['id'] == reality['primary_endpoint_id'],
            'target_type': target['type'], 'site_id': target.get('site_id'),
            'address': target.get('address', network['server']), 'port': network['server_port']})
    return result


def _addresses(host):
    try:
        return {ipaddress.ip_address(host)}
    except ValueError:
        result = subprocess.run([sys.executable, '-c', _DNS_SCRIPT, host],
                                capture_output=True, text=True, timeout=5)
        if result.returncode or len(result.stdout) > 8192:
            raise ValueError('DNS lookup failed')
        values = json.loads(result.stdout)
        if not isinstance(values, list) or not 1 <= len(values) <= 64:
            raise ValueError('Invalid DNS result')
        return {ipaddress.ip_address(value) for value in values}


def _local_addresses():
    from .management_web import _local_addresses as local_addresses
    return local_addresses()


def _external_addresses(server, address):
    try:
        address = validate_domain(address)
        resolved = _addresses(address)
        own = _addresses(server['public_address']) | _local_addresses()
        if not resolved or not own:
            raise ValueError('No resolved addresses')
        for value in resolved:
            _safe_address(str(value))
        if resolved & own:
            raise ValueError('Target points back to this server')
        return resolved
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        raise ManagementError('TARGET_CHECK_FAILED',
            '外部目标的全部 A / AAAA 必须为安全公网地址，且不能指向本机；请检查目标及 DNS。') from None


def resolve_external(server: Mapping, address: str) -> str:
    """Validate all addresses and select a fixed public address for this target."""
    resolved = _external_addresses(server, address)
    return str(min(resolved, key=lambda value: (value.version, int(value))))


def change(config: Mapping, kind: str, values: Mapping) -> dict:
    """Create a transition or explicitly select/retire an existing endpoint."""
    result = deepcopy(config)
    reality = result['reality'] = normalize_reality(config)
    if kind == 'reality.migrate':
        if not isinstance(values, Mapping):
            raise ValueError('REALITY migration must be an object')
        target_type = values.get('type')
        _shape(values, {'type', 'site_id', 'sni'} if target_type == 'site'
               else {'type', 'address', 'port', 'sni'})
        if len(reality['endpoints']) >= 8:
            raise ManagementError('VALIDATION', '最多保留 8 个 REALITY 入口；请先显式退役旧入口。')
        name = validate_domain(values['sni'])
        if any(target['server_name'] == name for target in reality['targets']):
            raise ManagementError('CONFLICT', '此 SNI 已有保留入口，请选择该入口作为当前发布入口。')
        target_id, endpoint_id = uuid.uuid4().hex, uuid.uuid4().hex
        if target_type == 'site':
            target = {'id': target_id, 'type': 'site', 'site_id': values['site_id'], 'server_name': name}
        elif target_type == 'external':
            if type(values['port']) is not int or values['port'] != 443:
                raise ValueError('External REALITY targets currently require HTTPS port 443')
            target = {'id': target_id, 'type': 'external', 'address': validate_domain(values['address']),
                      'port': 443, 'server_name': name,
                      'pinned_address': resolve_external(config['server'], values['address'])}
        else:
            raise ValueError('Unsupported REALITY target type')
        reserved = _reserved_ports(validate_spec(config['server']), normalize_web(config))
        occupied = reserved | {endpoint['listen_port'] for endpoint in reality['endpoints']}
        port = next((value for value in range(2444, 2544) if value not in occupied), None)
        if port is None:
            raise ManagementError('CONFLICT', '没有可分配的 REALITY 内部监听端口。')
        reality['targets'].append(target)
        reality['endpoints'].append({'id': endpoint_id, 'listener_id': 'reality-' + endpoint_id,
                                    'listen_port': port, 'target_id': target_id})
        reality['primary_endpoint_id'] = endpoint_id
    elif kind in {'reality.select', 'reality.retire'}:
        _shape(values, {'endpoint_id'})
        endpoint = get_endpoint(reality, values['endpoint_id'])
        if kind == 'reality.select':
            reality['primary_endpoint_id'] = endpoint['id']
        else:
            if endpoint['id'] == reality['primary_endpoint_id'] or len(reality['endpoints']) == 1:
                raise ManagementError('DEPENDENCY', '不能退役当前发布入口或最后一个 REALITY 入口；请先选择其他入口。')
            reality['endpoints'].remove(endpoint)
            reality['targets'].remove(get_target(reality, endpoint['target_id']))
    else:
        raise ValueError('Unsupported REALITY operation')
    result['reality'] = normalize_reality(result)
    return result


def probe_targets(config: Mapping) -> None:
    """Recheck all retained targets with pinned, trusted TLS 1.3/X25519 handshakes."""
    reality = normalize_reality(config)
    for endpoint in reality['endpoints']:
        target = get_target(reality, endpoint['target_id'])
        if target['type'] == 'external':
            resolved = _external_addresses(config['server'], target['address'])
            if ipaddress.ip_address(target['pinned_address']) not in resolved:
                raise ManagementError('TARGET_CHECK_FAILED', '外部目标 DNS 已变化，固定地址不再有效；请重新创建迁移计划。')
        network = endpoint_target(config, endpoint)
        address = network['server']
        connect = (f'[{address}]' if ':' in address else address) + ':' + str(network['server_port'])
        try:
            checked = subprocess.run(['openssl', 's_client', '-brief', '-connect', connect,
                '-servername', network['server_name'], '-verify_hostname', network['server_name'],
                '-verify_return_error', '-tls1_3', '-groups', 'X25519'],
                input='', text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
            if checked.returncode:
                raise ValueError('TLS check failed')
        except (OSError, subprocess.SubprocessError, ValueError):
            raise ManagementError('TARGET_CHECK_FAILED',
                'REALITY 目标 TLS 1.3、SNI、X25519 或信任链检查未通过；现有入口保留。') from None
