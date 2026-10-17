"""Pure subscription identities and references to the existing local snapshots."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import secrets

from . import hy2, nodes
from .proxy import validate_domain, validate_spec
from .publication import IDENTIFIER_PATTERN, MAX_PROFILES, TOKEN_PATTERN, PATH_PREFIX_PATTERN
from .websites import normalize_web


KINDS = frozenset('subscription.' + action for action in (
    'create', 'update', 'copy', 'enable', 'disable', 'alias.add', 'alias.retire', 'token.rotate'))
_METADATA = {'name', 'userinfo', 'update_interval_hours', 'reset_day', 'reset_at'}
_EDITABLE = _METADATA | {'host', 'path_prefix', 'node_ids', 'formats', 'overrides'}
_FIELDS = _EDITABLE | {'id', 'token', 'enabled', 'aliases'}
_OVERRIDES = {'mixed-port', 'log-level', 'mode', 'ipv6', 'allow-lan'}


def _shape(value, allowed, required=()):
    if not isinstance(value, Mapping) or set(value) - allowed or not set(required) <= set(value):
        raise ValueError('Subscription fields are missing or unsupported')


def _text(value, maximum, label):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid subscription ' + label)
    return value


def _identity(value):
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError('Invalid subscription identifier')
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid subscription metadata integer')
    return value


def validate_prefix(value):
    if (not isinstance(value, str) or len(value) > 128 or not PATH_PREFIX_PATTERN.fullmatch(value)
            or any(segment.startswith('__proxyhub') or segment in {'acme', 'status'}
                   for segment in value.split('/')[1:])):
        raise ValueError('Subscription path prefix requires safe literal path segments')
    return value


def allowed_hosts(config):
    """Only existing managed HTTPS hosts can receive a publication route."""
    server = validate_spec(config['server'])
    return {server['subscription_domain']} | {
        domain for site in normalize_web(config)['sites'] if site['enabled'] for domain in site['domains']}


def node_resources(config):
    server = validate_spec(config['server'])
    resources = [{'id': 'local-reality', 'name': server['node_name'], 'protocol': 'vless', 'enabled': True}]
    resource = hy2.normalize_hy2(config)
    if resource is not None:
        resources.append({'id': resource['id'], 'name': resource['name'], 'protocol': 'hysteria2',
                          'enabled': resource['enabled']})
    resources.extend({key: node[key] for key in ('id', 'name', 'protocol', 'enabled')}
                     for node in nodes.normalize_nodes(config))
    return resources


def available_node_ids(config):
    return [node['id'] for node in node_resources(config) if node['enabled']]


def selected_node_ids(config, profile):
    enabled = set(available_node_ids(config))
    return [identity for identity in (profile['node_ids'] or available_node_ids(config)) if identity in enabled]


def _overrides(value):
    _shape(value, _OVERRIDES)
    if 'mixed-port' in value:
        _integer(value['mixed-port'], 1, 65535)
    if 'log-level' in value and value['log-level'] not in ('silent', 'error', 'warning', 'info', 'debug'):
        raise ValueError('Invalid subscription log level override')
    if 'mode' in value and value['mode'] not in ('rule', 'global', 'direct'):
        raise ValueError('Invalid subscription mode override')
    if 'ipv6' in value and type(value['ipv6']) is not bool:
        raise ValueError('Invalid subscription IPv6 override')
    if 'allow-lan' in value and value['allow-lan'] is not False:
        raise ValueError('Subscription LAN access must remain disabled')
    return deepcopy(dict(value))


def _profile(config, value, *, default=False):
    _shape(value, _FIELDS, {'id', 'name', 'token', 'userinfo', 'update_interval_hours'})
    profile = deepcopy(dict(value))
    identity = _identity(profile['id'])
    if (identity == 'default') != default:
        raise ValueError('The legacy subscription owns the default identity')
    profile.setdefault('host', config['server']['subscription_domain'])
    profile.setdefault('path_prefix', '/sub')
    profile.setdefault('enabled', True)
    profile.setdefault('node_ids', None if default else [])
    profile.setdefault('formats', ['yaml', 'raw'])
    profile.setdefault('overrides', config['overrides'] if default else {})
    profile.setdefault('aliases', [])
    profile.setdefault('reset_day', None)
    profile.setdefault('reset_at', None)
    if not TOKEN_PATTERN.fullmatch(_text(profile['token'], 128, 'token')):
        raise ValueError('Invalid subscription token')
    name = _text(profile['name'], 200, 'name')
    if '/' in name or '\\' in name:
        raise ValueError('Invalid subscription download name')
    host = validate_domain(profile['host'])
    if host not in allowed_hosts(config):
        raise ValueError('Subscription host requires an enabled managed HTTPS domain')
    profile['host'] = host
    validate_prefix(profile['path_prefix'])
    if type(profile['enabled']) is not bool:
        raise ValueError('Subscription enabled must be a boolean')
    selected = profile['node_ids']
    known = {node['id'] for node in node_resources(config)}
    if selected is not None or not default:
        if (not isinstance(selected, list) or not selected or len(selected) > len(known)
                or any(not isinstance(item, str) or item not in known for item in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError('Subscription node selection contains empty, duplicate or unknown references')
    if profile['enabled'] and not selected_node_ids(config, profile):
        raise ValueError('Enabled subscription has no enabled selected nodes; disable it or select an available node')
    formats = profile['formats']
    if (not isinstance(formats, list) or not formats or any(item not in ('yaml', 'raw') for item in formats)
            or len(set(formats)) != len(formats)):
        raise ValueError('Subscription formats must select YAML and/or raw')
    profile['overrides'] = _overrides(profile['overrides'])
    userinfo = profile['userinfo']
    _shape(userinfo, {'upload', 'download', 'total', 'expire'})
    traffic = {'upload', 'download', 'total'}
    if set(userinfo) & traffic and not traffic <= set(userinfo):
        raise ValueError('Traffic metadata requires upload, download and total together')
    for item in userinfo.values():
        _integer(item, 0, 2**63 - 1)
    _integer(profile['update_interval_hours'], 1, 168)
    for field, minimum, maximum in (('reset_day', 1, 31), ('reset_at', 0, 2**63 - 1)):
        if profile[field] is not None:
            _integer(profile[field], minimum, maximum)
    aliases = profile['aliases']
    if not isinstance(aliases, list) or len(aliases) > 20:
        raise ValueError('Provide at most 20 subscription aliases')
    identities, routes = set(), {(host, profile['path_prefix'])}
    for alias in aliases:
        _shape(alias, {'id', 'host', 'path_prefix'}, {'id', 'host', 'path_prefix'})
        alias_id = _identity(alias['id'])
        alias['host'] = validate_domain(alias['host'])
        if alias['host'] not in allowed_hosts(config):
            raise ValueError('Subscription alias requires an enabled managed HTTPS domain')
        validate_prefix(alias['path_prefix'])
        route = (alias['host'], alias['path_prefix'])
        if alias_id in identities or route in routes:
            raise ValueError('Duplicate subscription alias identity or route')
        identities.add(alias_id)
        routes.add(route)
    return profile


def normalize(config):
    """Project legacy default fields without rewriting their stored representation."""
    if not isinstance(config, Mapping) or not {'server', 'subscription', 'overrides'} <= set(config):
        raise ValueError('Subscriptions require the legacy default configuration')
    legacy = deepcopy(config['subscription'])
    if not isinstance(legacy, Mapping):
        raise ValueError('The legacy subscription must be an object')
    if 'overrides' in legacy:
        raise ValueError('The default subscription overrides belong to the legacy root field')
    extras = config.get('subscriptions', [])
    if not isinstance(extras, list) or len(extras) >= MAX_PROFILES:
        raise ValueError('Subscription count exceeds the publication limit')
    profiles = [_profile(config, legacy, default=True), *[_profile(config, value) for value in extras]]
    identities, tokens = set(), set()
    for profile in profiles:
        if profile['id'] in identities or profile['token'] in tokens:
            raise ValueError('Duplicate subscription identity or token')
        identities.add(profile['id'])
        tokens.add(profile['token'])
    return profiles


def get_profile(config, subscription_id='default'):
    identity = _identity(subscription_id)
    return next((profile for profile in normalize(config) if profile['id'] == identity), None) or _missing()


def _missing():
    raise ValueError('Subscription identifier does not exist')


def public_resources(config):
    return {'profiles': [{key: deepcopy(value) for key, value in profile.items() if key != 'token'}
                         for profile in normalize(config)], 'nodes': node_resources(config)}


def _store(config, profile):
    if profile['id'] == 'default':
        # Keep the original default fields in their original locations.
        config['overrides'] = profile['overrides']
        config['subscription'] = {key: value for key, value in profile.items() if key != 'overrides'}
    else:
        values = config.setdefault('subscriptions', [])
        index = next((index for index, item in enumerate(values) if item['id'] == profile['id']), None)
        if index is None:
            values.append(profile)
        else:
            values[index] = profile


def change(config, kind, values):
    if kind not in KINDS:
        raise ValueError('Unsupported subscription operation')
    if not isinstance(values, Mapping):
        raise ValueError('Subscription operation requires an object')
    result = deepcopy(config)
    before = normalize(result)
    if kind == 'subscription.create':
        _shape(values, _EDITABLE, {'name', 'node_ids'})
        profile = {'id': secrets.token_hex(16), 'token': secrets.token_urlsafe(32),
                   'userinfo': {}, 'update_interval_hours': 12, **deepcopy(values)}
    else:
        identity = values.get('subscription_id', 'default')
        profile = get_profile(result, identity)
        if kind == 'subscription.update':
            _shape(values, _EDITABLE | {'subscription_id'})
            if not set(values) - {'subscription_id'}:
                raise ValueError('Subscription update requires editable fields')
            old_route = (profile['host'], profile['path_prefix'])
            profile.update({key: deepcopy(value) for key, value in values.items() if key != 'subscription_id'})
            if old_route != (profile['host'], profile['path_prefix']):
                profile['aliases'] = [alias for alias in profile['aliases']
                                      if (alias['host'], alias['path_prefix']) != (profile['host'], profile['path_prefix'])]
                profile['aliases'].append({'id': secrets.token_hex(16), 'host': old_route[0], 'path_prefix': old_route[1]})
        elif kind == 'subscription.copy':
            _shape(values, {'subscription_id', 'name'}, {'subscription_id'})
            profile.update(id=secrets.token_hex(16), token=secrets.token_urlsafe(32), aliases=[],
                           node_ids=selected_node_ids(result, profile))
            if 'name' in values:
                profile['name'] = values['name']
        elif kind == 'subscription.alias.add':
            _shape(values, {'subscription_id', 'host', 'path_prefix'}, {'subscription_id', 'host', 'path_prefix'})
            profile['aliases'].append({'id': secrets.token_hex(16), 'host': values['host'], 'path_prefix': values['path_prefix']})
        elif kind == 'subscription.alias.retire':
            _shape(values, {'subscription_id', 'alias_id'}, {'subscription_id', 'alias_id'})
            retired = next((alias for alias in profile['aliases'] if alias['id'] == values['alias_id']), None)
            if retired is None:
                raise ValueError('Subscription alias identifier does not exist')
            route = (retired['host'], retired['path_prefix'])
            for other in before:
                if other['id'] != identity:
                    if any((entry['host'], entry['path_prefix']) == route for entry in [other, *other['aliases']]):
                        raise ValueError('Subscription alias route is still referenced by another subscription')
            profile['aliases'] = [alias for alias in profile['aliases'] if alias['id'] != values['alias_id']]
        else:
            _shape(values, {'subscription_id'})
            if kind == 'subscription.token.rotate':
                profile['token'] = secrets.token_urlsafe(32)
            else:
                profile['enabled'] = kind == 'subscription.enable'
    _store(result, profile)
    if normalize(result) == before:
        raise ValueError('Subscription operation does not change a resource')
    return result
