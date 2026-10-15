"""Pure website, TLS listener and certificate-binding resource contracts."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import PurePosixPath
import re

from .proxy import _text, validate_domain, validate_managed_certificate_path, validate_spec


_ID = re.compile(r'(?:default|[0-9a-f]{32})\Z')
_VERSION = re.compile(r'[0-9a-f]{32}\Z')
_SITE_FIELDS = {'id', 'listener_id', 'tls_port', 'domains', 'title', 'enabled',
                'certificate_id', 'content_version'}
_CERTIFICATE_FIELDS = {'id', 'name', 'domains', 'certificate_path', 'key_path'}
_PORT_FIELDS = ('http_port', 'public_port', 'reality_port', 'site_tls_port',
                'subscription_tls_port', 'publisher_port')


def _identity(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f'{label} must be default or 32 lowercase hexadecimal digits')
    return value


def _shape(value, fields, label):
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f'{label} fields are missing or unsupported')


def _domains(value, *, empty=False):
    if not isinstance(value, list) or not (0 if empty else 1) <= len(value) <= 10:
        raise ValueError('Domains must be a list of at most 10 names')
    names = [validate_domain(domain) for domain in value]
    if len(set(names)) != len(names):
        raise ValueError('Duplicate domain binding')
    return names


def _content_version(value):
    if value is not None and (not isinstance(value, str) or not _VERSION.fullmatch(value)):
        raise ValueError('Website content version must be null or 32 lowercase hexadecimal digits')
    return value


def get_site(web: dict, identity: str) -> dict:
    _identity(identity, 'Website ID')
    for site in web['sites']:
        if site['id'] == identity:
            return site
    raise ValueError('Unknown website ID')


def get_certificate(web: dict, identity: str) -> dict:
    _identity(identity, 'Certificate binding ID')
    for certificate in web['certificates']:
        if certificate['id'] == identity:
            return certificate
    raise ValueError('Unknown certificate binding ID')


def site_directory(identity: str) -> str:
    _identity(identity, 'Website ID')
    return 'site' if identity == 'default' else 'sites/' + identity


def normalize_web(config: Mapping) -> dict:
    """Return a validated independent web object, including legacy defaults.

    No files, DNS records or running services are inspected. The caller retains
    the previous committed configuration to enforce immutable resource ports.
    """
    if not isinstance(config, Mapping) or 'server' not in config:
        raise ValueError('Website resources require a server configuration')
    server = validate_spec(config['server'])
    if 'web' not in config:
        return {
            'sites': [{'id': 'default', 'listener_id': 'site-default',
                       'tls_port': server['site_tls_port'], 'domains': [server['site_domain']],
                       'title': server['site_title'], 'enabled': True, 'certificate_id': 'default',
                       'content_version': _content_version(config.get('website_content'))}],
            'certificates': [{'id': 'default', 'name': '默认网站与订阅证书',
                              'domains': [server['site_domain'], server['subscription_domain']],
                              'certificate_path': server['certificate_path'], 'key_path': server['key_path']}],
            'subscription_certificate_id': 'default',
        }
    web = deepcopy(config['web'])
    _shape(web, {'sites', 'certificates', 'subscription_certificate_id'}, 'Website resource')
    if not isinstance(web['sites'], list) or not 1 <= len(web['sites']) <= 16:
        raise ValueError('Provide 1–16 websites')
    if not isinstance(web['certificates'], list) or not 1 <= len(web['certificates']) <= 32:
        raise ValueError('Provide 1–32 certificate bindings')
    certificate_ids = set()
    for certificate in web['certificates']:
        _shape(certificate, _CERTIFICATE_FIELDS, 'Certificate binding')
        identity = _identity(certificate['id'], 'Certificate binding ID')
        if identity in certificate_ids:
            raise ValueError('Duplicate certificate binding ID')
        certificate_ids.add(identity)
        certificate['name'] = _text(certificate['name'], 'certificate name')
        certificate['domains'] = _domains(certificate['domains'])
        for field in ('certificate_path', 'key_path'):
            certificate[field] = validate_managed_certificate_path(certificate[field], field)
        if PurePosixPath(certificate['certificate_path']).parent != PurePosixPath(certificate['key_path']).parent:
            raise ValueError('Certificate and key must belong to the same managed pair')
    subscription = get_certificate(web, web['subscription_certificate_id'])
    if server['subscription_domain'] not in subscription['domains']:
        raise ValueError('Subscription certificate does not cover its domain')
    site_ids, listeners, ports, domains = set(), set(), set(), {server['subscription_domain']}
    reserved_ports = {server[field] for field in _PORT_FIELDS}
    for site in web['sites']:
        _shape(site, _SITE_FIELDS, 'Website')
        identity = _identity(site['id'], 'Website ID')
        if identity in site_ids:
            raise ValueError('Duplicate website ID')
        site_ids.add(identity)
        if site['listener_id'] != 'site-' + identity or site['listener_id'] in listeners:
            raise ValueError('Website listener ID must match its stable website ID')
        listeners.add(site['listener_id'])
        port = site['tls_port']
        if type(port) is not int or not 1 <= port <= 65535 or port in ports:
            raise ValueError('Website TLS listener port is invalid or duplicated')
        if port in reserved_ports and not (identity == 'default' and port == server['site_tls_port']):
            raise ValueError('Website TLS listener conflicts with a reserved port')
        ports.add(port)
        if type(site['enabled']) is not bool:
            raise ValueError('Website enabled must be a boolean')
        site['title'] = _text(site['title'], 'website title')
        site['domains'] = _domains(site['domains'], empty=not site['enabled'])
        if domains.intersection(site['domains']):
            raise ValueError('Website domain conflicts with another SNI binding')
        domains.update(site['domains'])
        site['content_version'] = _content_version(site['content_version'])
        if site['certificate_id'] is None:
            if site['enabled']:
                raise ValueError('An enabled website requires a certificate binding')
        else:
            certificate = get_certificate(web, site['certificate_id'])
            if not set(site['domains']) <= set(certificate['domains']):
                raise ValueError('Website certificate does not cover its domain bindings')
        if identity == 'default' and (not site['enabled'] or port != server['site_tls_port']
                                      or not site['domains'] or site['domains'][0] != server['site_domain']):
            raise ValueError('The default website must retain its REALITY SNI and TLS listener')
    if 'default' not in site_ids:
        raise ValueError('The default REALITY target website is required')
    return web


def sync_default(config: Mapping) -> dict:
    """Return a new full config with legacy fields reflecting the default site."""
    result = deepcopy(config)
    web = normalize_web(result)
    site = get_site(web, 'default')
    certificate = get_certificate(web, site['certificate_id'])
    result['web'] = web
    result['server']['site_title'] = site['title']
    for field in ('certificate_path', 'key_path'):
        result['server'][field] = certificate[field]
    if site['content_version'] is None:
        result.pop('website_content', None)
    else:
        result['website_content'] = site['content_version']
    return result


def consumers(web: dict, server: Mapping, certificate_id: str, reality: dict | None = None) -> list[dict]:
    """Describe binding dependencies, including disabled sites and REALITY."""
    get_certificate(web, certificate_id)
    result = []
    for site in web['sites']:
        if site['certificate_id'] == certificate_id:
            for domain in site['domains'] or [None]:
                result.append({'kind': 'website', 'id': site['id'], 'label': site['title'] + ' HTTPS',
                               'domain': domain, 'port': site['tls_port'], 'enabled': site['enabled']})
    if web['subscription_certificate_id'] == certificate_id:
        result.append({'kind': 'subscription', 'id': 'subscription-tls', 'label': '订阅 HTTPS',
                       'domain': server['subscription_domain'], 'port': server['subscription_tls_port']})
    if reality is None:
        default = get_site(web, 'default')
        if default['certificate_id'] == certificate_id:
            result.append({'kind': 'reality', 'id': 'local-reality', 'label': 'REALITY 本机握手目标（间接）',
                           'domain': server['site_domain'], 'port': default['tls_port']})
    else:
        targets = {target['id']: target for target in reality['targets']}
        for endpoint in reality['endpoints']:
            target = targets[endpoint['target_id']]
            if target['type'] != 'site':
                continue
            site = get_site(web, target['site_id'])
            if site['certificate_id'] == certificate_id:
                result.append({'kind': 'reality', 'id': endpoint['id'],
                               'label': 'REALITY ' + endpoint['id'] + ' 本机握手目标（网站 ' + site['id'] + '，间接）',
                               'domain': target['server_name'], 'port': site['tls_port']})
    return result


def site_dependencies(web: dict, server: Mapping, site_id: str, reality: dict | None = None,
                      domain: str | None = None) -> list[str]:
    site = get_site(web, site_id)
    if reality is None:
        if site['id'] == 'default' and domain in (None, server['site_domain']):
            return ['REALITY 节点 local-reality 引用网站 default、域名 ' + server['site_domain']
                    + ' 与内部监听 ' + site['listener_id']]
        return []
    targets = {target['id']: target for target in reality['targets']}
    result = []
    for endpoint in reality['endpoints']:
        target = targets[endpoint['target_id']]
        if (target['type'] == 'site' and target['site_id'] == site_id
                and domain in (None, target['server_name'])):
            result.append('REALITY 入口 ' + endpoint['id'] + ' 引用网站 ' + site_id + '、域名 '
                          + target['server_name'] + ' 与内部监听 ' + site['listener_id'])
    return result
