"""Typed website and certificate-binding changes, without service activation."""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import uuid

from .core import atomic_write
from .management_errors import ManagementError
from .proxy import _text, validate_domain
from .site import validate_certificate
from .websites import (consumers, get_certificate, get_site, normalize_web,
                       site_dependencies, sync_default)

KINDS = frozenset({
    'website.create', 'website.enable', 'website.disable', 'website.delete',
    'website.domain.bind', 'website.domain.unbind', 'website.certificate.bind',
    'certificate.add', 'certificate.import', 'certificate.delete',
})
_ID = re.compile(r'(?:default|[0-9a-f]{32})\Z')
_PORT_FIELDS = ('http_port', 'public_port', 'reality_port', 'site_tls_port',
                'subscription_tls_port', 'publisher_port')


def _shape(value, fields, required=None):
    required = fields if required is None else required
    if not isinstance(value, dict) or set(value) - set(fields) or not set(required) <= set(value):
        raise ManagementError('VALIDATION', '请求字段缺失或包含不支持的字段。')


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ManagementError('VALIDATION', '网站或证书绑定编号无效。')
    return value


def _domains(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ManagementError('VALIDATION', '证书需要 1–10 个不重复的有效域名。')
    result = [validate_domain(domain) for domain in value]
    if len(set(result)) != len(result):
        raise ManagementError('VALIDATION', '证书需要 1–10 个不重复的有效域名。')
    return result


def _addresses(host):
    try:
        return {ipaddress.ip_address(host)}
    except ValueError:
        return {ipaddress.ip_address(entry[4][0].split('%')[0])
                for entry in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                if entry[0] in (socket.AF_INET, socket.AF_INET6)}


def _local_addresses():
    """Inspect interface configuration locally; never probe a supplied URL."""
    try:
        result = subprocess.run(['ip', '-j', 'address', 'show'], capture_output=True,
                                check=False, timeout=5)
        if result.returncode:
            return set()
        interfaces = json.loads(result.stdout)
        return {address for interface in interfaces for item in interface.get('addr_info', [])
                if item.get('scope') == 'global' and 'local' in item
                if (address := ipaddress.ip_address(item['local'])).is_global}
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, AttributeError):
        return set()


def check_domain(server: dict, domain: str) -> None:
    """Require every resolved A/AAAA address to identify this configured server."""
    try:
        domain = validate_domain(domain)
        resolved = _addresses(domain)
        allowed = _addresses(server['public_address']) | _local_addresses()
        if not resolved or not allowed or not resolved <= allowed:
            raise ValueError('DNS does not identify this server')
    except (OSError, KeyError, TypeError, ValueError):
        raise ManagementError('DNS_CHECK_FAILED', '域名的全部 A / AAAA 记录必须指向本机公网地址；请检查 DNS 后重新预览。') from None


def _covers(certificate, domains):
    domains = domains or certificate['domains']
    if not set(domains) <= set(certificate['domains']):
        raise ManagementError('VALIDATION', '证书绑定声明的域名未覆盖网站全部域名。')
    try:
        validate_certificate(certificate['certificate_path'], certificate['key_path'], domains)
    except (OSError, ValueError):
        raise ManagementError('VALIDATION', '证书或私钥无效，或证书实际 SAN 未覆盖网站全部域名。') from None


def _import(values, domains, root):
    from .certificates import import_pair
    for name in ('certificate_pem', 'private_key_pem'):
        value = values[name]
        if not isinstance(value, str) or not 1 <= len(value.encode()) <= 100000:
            raise ManagementError('VALIDATION', '证书和私钥需要有效 PEM，单个输入不得超过 100KB。')
    parent = Path(root)/'certificates'
    if parent.is_symlink():
        raise ManagementError('VALIDATION', '证书存储目录无效。')
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.panel-import-', dir=parent) as temporary:
        certificate, key = Path(temporary)/'cert.pem', Path(temporary)/'key.pem'
        atomic_write(certificate, values['certificate_pem'].encode(), 0o600)
        atomic_write(key, values['private_key_pem'].encode(), 0o600)
        try:
            return import_pair(certificate, key, domains)
        except (OSError, ValueError):
            raise ManagementError('VALIDATION', '证书或私钥校验未通过，必须实际覆盖声明域名与全部消费者域名。') from None


def _dependencies(labels):
    if labels:
        raise ManagementError('DEPENDENCY', '仍被以下消费者引用：' + '；'.join(dict.fromkeys(labels)) + '。')


def change(config: dict, kind: str, values: dict, root: Path) -> dict:
    """Return a candidate copy and public preview; PEM remains in private storage."""
    try:
        return _change(config, kind, values, root)
    except ManagementError:
        raise
    except (OSError, KeyError, TypeError, ValueError):
        raise ManagementError('VALIDATION', '网站或证书字段校验未通过，请检查编号、域名、标题与资源限额。') from None


def _change(config, kind, values, root):
    if kind not in KINDS:
        raise ManagementError('UNSUPPORTED', '当前不支持此网站或证书变更。')
    result = deepcopy(config)
    web = normalize_web(result)
    server = result['server']
    reality = result.get('reality')
    hy2 = result.get('hy2')
    result['web'] = web
    changes, warnings = [], []

    if kind == 'website.create':
        _shape(values, ('title', 'domain', 'certificate_id'))
        if len(web['sites']) >= 16:
            raise ManagementError('VALIDATION', '最多支持 16 个网站。')
        title = _text(values['title'], 'website title')
        domain = validate_domain(values['domain'])
        certificate = get_certificate(web, _identity(values['certificate_id']))
        check_domain(server, domain)
        _covers(certificate, [domain])
        used = {server[field] for field in _PORT_FIELDS} | {site['tls_port'] for site in web['sites']}
        port = next((port for port in range(8445, 8545) if port not in used), None)
        if port is None:
            raise ManagementError('CONFLICT', '网站内部监听端口候选已用完。')
        identity = uuid.uuid4().hex
        web['sites'].append({'id': identity, 'listener_id': 'site-' + identity, 'tls_port': port,
                             'domains': [domain], 'title': title, 'enabled': True,
                             'certificate_id': certificate['id'], 'content_version': None})
        changes = [{'label': '新增网站', 'before': None, 'after': title},
                   {'label': '网站编号', 'before': None, 'after': identity},
                   {'label': '域名', 'before': None, 'after': domain}]
    elif kind in {'website.enable', 'website.disable', 'website.delete'}:
        _shape(values, ('site_id',))
        site = get_site(web, _identity(values['site_id']))
        if kind != 'website.enable':
            _dependencies(site_dependencies(web, server, site['id'], reality))
            if site['id'] == 'default':
                _dependencies(['默认网站为原配置兼容资源，须保持启用并保留'])
        if kind == 'website.delete':
            if site['enabled']:
                _dependencies([site['title'] + ' HTTPS（网站启用中，请先停用）'])
            web['sites'].remove(site)
            changes = [{'label': '删除网站', 'before': site['title'], 'after': None}]
        else:
            enabled = kind == 'website.enable'
            if enabled:
                if not site['domains'] or site['certificate_id'] is None:
                    raise ManagementError('VALIDATION', '启用网站前必须绑定域名与有效证书。')
                for domain in site['domains']:
                    check_domain(server, domain)
                _covers(get_certificate(web, site['certificate_id']), site['domains'])
            changes = [{'label': site['title'] + ' HTTPS', 'before': site['enabled'], 'after': enabled}]
            site['enabled'] = enabled
    elif kind in {'website.domain.bind', 'website.domain.unbind'}:
        _shape(values, ('site_id', 'domain'))
        site = get_site(web, _identity(values['site_id']))
        domain = validate_domain(values['domain'])
        before = list(site['domains'])
        if kind == 'website.domain.bind':
            if domain in before:
                raise ManagementError('CONFLICT', '该域名已绑定到此网站。')
            if len(before) >= 10:
                raise ManagementError('VALIDATION', '单个网站最多支持 10 个域名。')
            check_domain(server, domain)
            if site['certificate_id'] is None:
                raise ManagementError('VALIDATION', '绑定域名前必须选择有效证书。')
            _covers(get_certificate(web, site['certificate_id']), before + [domain])
            site['domains'].append(domain)
        else:
            if domain not in before:
                raise ManagementError('VALIDATION', '该域名未绑定到此网站。')
            _dependencies(site_dependencies(web, server, site['id'], reality, domain))
            if site['id'] == 'default' and domain == server['site_domain']:
                _dependencies(['默认网站首域名 ' + domain + ' 为原配置兼容字段，须保留'])
            if site['enabled'] and len(before) == 1:
                _dependencies([site['title'] + ' HTTPS（解绑最后域名前请先停用网站）'])
            site['domains'].remove(domain)
        changes = [{'label': site['title'] + ' 域名', 'before': before, 'after': list(site['domains'])}]
    elif kind == 'website.certificate.bind':
        _shape(values, ('site_id', 'certificate_id'))
        site = get_site(web, _identity(values['site_id']))
        certificate = get_certificate(web, _identity(values['certificate_id']))
        _covers(certificate, site['domains'])
        changes = [{'label': site['title'] + ' 证书绑定', 'before': site['certificate_id'], 'after': certificate['id']}]
        site['certificate_id'] = certificate['id']
    elif kind == 'certificate.add':
        _shape(values, ('name', 'domains', 'certificate_pem', 'private_key_pem'))
        if len(web['certificates']) >= 32:
            raise ManagementError('VALIDATION', '最多支持 32 个证书绑定。')
        name = _text(values['name'], 'certificate name')
        domains = _domains(values['domains'])
        imported = _import(values, domains, root)
        certificate = {'id': uuid.uuid4().hex, 'name': name, 'domains': domains,
                       'certificate_path': imported['certificate_path'], 'key_path': imported['key_path']}
        web['certificates'].append(certificate)
        changes = [{'label': '新增证书绑定', 'before': None, 'after': name},
                   {'label': '证书绑定编号', 'before': None, 'after': certificate['id']},
                   {'label': '证书域名', 'before': None, 'after': domains}]
        warnings = ['导入证书由原签发流程维护，不接管自动续期。', '私钥不进入预览或任务结果。']
    elif kind == 'certificate.import':
        _shape(values, ('certificate_id', 'certificate_pem', 'private_key_pem'), ('certificate_pem', 'private_key_pem'))
        identity = _identity(values.get('certificate_id', get_site(web, 'default')['certificate_id']))
        certificate = get_certificate(web, identity)
        required = list(dict.fromkeys(certificate['domains'] + [consumer['domain']
                        for consumer in consumers(web, server, identity, reality, hy2) if consumer['domain'] is not None]))
        imported = _import(values, required, root)
        certificate.update(certificate_path=imported['certificate_path'], key_path=imported['key_path'])
        changes = [{'label': '替换证书绑定', 'before': certificate['name'], 'after': certificate['name']},
                   {'label': '证书 SHA256', 'before': '当前绑定证书', 'after': imported['sha256']},
                   {'label': '有效期至', 'before': None, 'after': imported['not_after']}]
        from .certificates import acme_source
        ownership = ('此证书已有该绑定的受管 HTTP-01 签发记录，保留自动续期归属。'
                     if acme_source(certificate) else '导入证书由原签发流程维护，不接管自动续期。')
        warnings = [ownership, '私钥不进入预览或任务结果。']
    elif kind == 'certificate.delete':
        _shape(values, ('certificate_id',))
        certificate = get_certificate(web, _identity(values['certificate_id']))
        _dependencies([consumer['label'] for consumer in consumers(web, server, certificate['id'], reality, hy2)])
        web['certificates'].remove(certificate)
        changes = [{'label': '删除证书绑定', 'before': certificate['name'], 'after': None}]
    return {'config': sync_default(result), 'changes': changes, 'warnings': warnings}
