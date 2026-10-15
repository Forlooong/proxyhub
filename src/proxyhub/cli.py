"""Namespaced CLI. Plans are private; help never needs root or touches state."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import secrets
import sys
import uuid

from . import __version__
from .core import OperationError, atomic_write, canonical, require_root, run


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='proxyhub', description='ProxyHub: independent host, site, proxy and subscription management')
    root.add_argument('--version', action='version', version=__version__)
    modules = root.add_subparsers(dest='module')
    help_command = modules.add_parser('help', help='Show help for a module or command')
    help_command.add_argument('topic', nargs='*')
    modules.add_parser('install-services', help='Install only ProxyHub accounts, units and command')
    host = modules.add_parser('host', help='Host access operations').add_subparsers(dest='operation')
    policy = host.add_parser('enable-key-auth', help='Preview enabling public-key authentication without changing password/port')
    policy.add_argument('--apply', action='store_true')
    ssh = host.add_parser('append-key', help='Prepare an append-only SSH public key plan')
    ssh.add_argument('--account', required=True)
    ssh.add_argument('--public-key-file', required=True, type=Path)
    ssh.add_argument('--authorized-keys-file', required=True)
    for argument in ('addr', 'host', 'laddr', 'lport'):
        ssh.add_argument('--'+argument, required=True)
    site = modules.add_parser('site', help='Website content and certificate operations').add_subparsers(dest='operation')
    website = site.add_parser('website', help='Manage independent websites and their dependencies').add_subparsers(dest='action')
    website.add_parser('list', help='List websites and certificate dependencies')
    create = website.add_parser('create', help='Prepare an independent website')
    create.add_argument('--title', required=True)
    create.add_argument('--domain', required=True)
    create.add_argument('--certificate-id', required=True)
    for action in ('enable', 'disable', 'delete'):
        command = website.add_parser(action, help='Prepare website '+action)
        command.add_argument('--site-id', required=True)
    domains = website.add_parser('domain', help='Manage website domain bindings').add_subparsers(dest='resource_action')
    for action in ('bind', 'unbind'):
        command = domains.add_parser(action, help='Prepare domain '+action)
        command.add_argument('--site-id', required=True)
        command.add_argument('--domain', required=True)
    bindings = website.add_parser('certificate', help='Manage website certificate bindings').add_subparsers(dest='resource_action')
    binding = bindings.add_parser('bind', help='Prepare binding a certificate to a website')
    binding.add_argument('--site-id', required=True)
    binding.add_argument('--certificate-id', required=True)
    content = site.add_parser('content', help='Preview, version and restore static website content').add_subparsers(dest='action')
    upload = content.add_parser('upload', help='Prepare a static ZIP upload; apply the returned plan explicitly')
    upload.add_argument('--archive', type=Path, required=True)
    upload.add_argument('--site-id', help='Website ID; omitted means the default website')
    versions = content.add_parser('versions', help='List website content versions and the current version')
    versions.add_argument('--site-id', help='Website ID; omitted means the default website')
    restore = content.add_parser('restore', help='Prepare restoring an uploaded version or template')
    restore.add_argument('--version', required=True)
    restore.add_argument('--site-id', help='Website ID; omitted means the default website')
    preview = content.add_parser('preview', help='Write a prepared plan HTML preview to a private file')
    preview.add_argument('--plan-id', required=True)
    preview.add_argument('--output', type=Path, required=True)
    certificate = site.add_parser('certificate', help='Issue/import a versioned certificate').add_subparsers(dest='action')
    issue = certificate.add_parser('issue', help='HTTP-01 initial issue; port 80 must be free')
    issue.add_argument('--domain', action='append', required=True)
    issue.add_argument('--webroot', action='store_true')
    imported = certificate.add_parser('import', help='Import and validate a matching certificate and private key')
    imported.add_argument('--certificate', type=Path, required=True)
    imported.add_argument('--key', type=Path, required=True)
    imported.add_argument('--domain', action='append', required=True)
    certificate.add_parser('list', help='List certificates and their consumers')
    added = certificate.add_parser('add', help='Prepare adding a manually managed certificate')
    added.add_argument('--name', required=True)
    added.add_argument('--domain', action='append', required=True)
    added.add_argument('--certificate', type=Path, required=True)
    added.add_argument('--key', type=Path, required=True)
    replace = certificate.add_parser('replace', help='Prepare replacing a certificate without changing its ID')
    replace.add_argument('--certificate-id', required=True)
    replace.add_argument('--certificate', type=Path, required=True)
    replace.add_argument('--key', type=Path, required=True)
    requested = certificate.add_parser('request', help='Prepare a managed HTTP-01 certificate request')
    requested.add_argument('--name', required=True)
    requested.add_argument('--domain', action='append', required=True)
    for action in ('renew', 'delete'):
        command = certificate.add_parser(action, help='Prepare certificate '+action)
        command.add_argument('--certificate-id', required=True)
    proxy = modules.add_parser('proxy', help='Proxy nodes and REALITY entry migration').add_subparsers(dest='operation')
    reality = proxy.add_parser('reality', help='Manage REALITY targets while retaining existing entries').add_subparsers(dest='action')
    reality.add_parser('list', help='List primary and retained REALITY entries')
    migrate = reality.add_parser('migrate', help='Prepare a new REALITY entry and retain the old one')
    target = migrate.add_mutually_exclusive_group(required=True)
    target.add_argument('--site-id', help='Enabled managed website ID')
    target.add_argument('--external-host', help='External TLS target address, separate from SNI; port 443')
    migrate.add_argument('--sni', required=True)
    for action in ('select', 'retire'):
        command = reality.add_parser(action, help='Prepare '+action+' of a retained REALITY entry')
        command.add_argument('--endpoint-id', required=True)
    deploy = modules.add_parser('deployment', help='Single-node generation, validation, activation and recovery').add_subparsers(dest='operation')
    init = deploy.add_parser('init', help='Write a private initial config with fresh node and link credentials')
    for field in ('site-domain', 'subscription-domain', 'public-address', 'certificate', 'key'):
        init.add_argument('--'+field, required=True)
    init.add_argument('--output', required=True, type=Path)
    plan = deploy.add_parser('plan', help='Validate immutable candidate using pinned runtimes')
    plan.add_argument('--config', required=True, type=Path)
    deploy.add_parser('recover', help='Recover an interrupted ProxyHub activation')
    renew = deploy.add_parser('renew', help='Renew managed HTTP-01 certificate and activate it if changed')
    apply = modules.add_parser('apply', help='Apply an explicitly prepared plan')
    apply.add_argument('plan_id')
    sub = modules.add_parser('sub', help='Subscription access').add_subparsers(dest='operation')
    urls = sub.add_parser('urls', help='Show SECRET client links from active config (root only)')
    urls.add_argument('--output', type=Path, help='Write links to a private file instead of stdout')
    panel = modules.add_parser('panel', help='Private management panel over an SSH tunnel').add_subparsers(dest='operation')
    panel.add_parser('install', help='Install and start the loopback gateway and privileged manager')
    access = panel.add_parser('access', help='Show SECRET panel credential and tunnel instructions (root only)')
    access.add_argument('--output', type=Path, help='Write access details to a private file instead of stdout')
    request = panel.add_parser('request', help='Call a typed manager operation through its local socket')
    request.add_argument('--operation', dest='rpc_operation', required=True)
    request.add_argument('--payload-file', type=Path, help='JSON object file; omitted means an empty object')
    return root


def _help(root: argparse.ArgumentParser, topics: list[str]) -> None:
    selected = root
    for topic in topics:
        subparsers = next((action for action in selected._actions if isinstance(action, argparse._SubParsersAction)), None)
        if subparsers is None or topic not in subparsers.choices:
            root.error('Unknown help topic')
        selected = subparsers.choices[topic]
    selected.print_help()


def _initial_config(args) -> dict:
    if args.output.exists() or args.output.is_symlink():
        raise OperationError('Output already exists; choose a new private configuration path')
    from .proxy import validate_spec
    generated = run(['/opt/proxyhub/bin/sing-box', 'generate', 'reality-keypair'])
    pair = dict(line.split(':', 1) for line in generated.splitlines() if ':' in line)
    server = validate_spec({
        'site_domain': args.site_domain, 'subscription_domain': args.subscription_domain,
        'public_address': args.public_address, 'uuid': str(uuid.uuid4()),
        'private_key': pair['PrivateKey'].strip(), 'public_key': pair['PublicKey'].strip(),
        'short_id': secrets.token_hex(8), 'certificate_path': args.certificate, 'key_path': args.key,
    })
    config = {'server': server, 'subscription': {'id': 'default', 'token': secrets.token_urlsafe(32),
              'name': 'ProxyHub', 'userinfo': {}, 'update_interval_hours': 6}, 'overrides': {}}
    atomic_write(args.output, canonical(config))
    return {'status': 'created', 'private_config': str(args.output), 'traffic_metadata': 'unknown'}


def _read_pem(path: Path) -> str:
    with path.open('rb') as source:
        content = source.read(100 * 1024 + 1)
    if not content or len(content) > 100 * 1024:
        raise OperationError('PEM file must be nonempty and no larger than 100 KiB')
    return content.decode('utf-8')


def _site_plan(service, kind: str, values: dict) -> dict:
    with service.state() as state:
        revision = state.revision
    return service.create_plan(kind, revision, values)


def execute(args) -> dict:
    require_root()
    if args.module == 'install-services':
        from .installer import install
        return install()
    if args.module == 'panel':
        if args.operation == 'install':
            from .panel_install import install
            return install()
        if args.operation == 'access':
            from .panel_install import access
            result = access()
            if args.output:
                atomic_write(args.output, canonical(result))
                return {'status': 'written', 'private_access': str(args.output)}
            return result
        from .rpc import call
        payload = json.loads(args.payload_file.read_text(encoding='utf-8')) if args.payload_file else {}
        return call(args.rpc_operation, payload)
    if args.module == 'host' and args.operation == 'enable-key-auth':
        from .host.policy import enable_key_auth
        return enable_key_auth(apply=args.apply)
    if args.module == 'proxy':
        from .management import OperationService
        service = OperationService(synchronous=True)
        try:
            if args.action == 'list':
                return service.handle('reality.resources', {})
            if args.action == 'migrate':
                values = {'type': 'site', 'site_id': args.site_id, 'sni': args.sni} if args.site_id else {
                    'type': 'external', 'address': args.external_host, 'port': 443, 'sni': args.sni}
            else:
                values = {'endpoint_id': args.endpoint_id}
            return _site_plan(service, 'reality.' + args.action, values)
        finally:
            service.close()
    if args.module == 'site' and args.operation == 'content':
        from .management import OperationService
        service = OperationService(synchronous=True)
        try:
            if args.action == 'versions':
                return service.handle('website.content.versions', {'site_id': args.site_id} if args.site_id else {})
            if args.action == 'preview':
                result = service.handle('website.content.preview', {'plan_id': args.plan_id})
                atomic_write(args.output, result['html'].encode('utf-8'), 0o600)
                return {'status': 'written', 'private_preview': str(args.output),
                        'file_count': result['file_count'], 'total_bytes': result['total_bytes']}
            if args.action == 'upload':
                with args.archive.open('rb') as source:
                    archive = source.read(180 * 1024 + 1)
                if not archive or len(archive) > 180 * 1024:
                    raise OperationError('ZIP archive must be nonempty and no larger than 180 KiB')
                kind, values = 'website.content.upload', {'archive_base64': base64.b64encode(archive).decode('ascii')}
            else:
                kind, values = 'website.content.restore', {'version_id': args.version}
            if args.site_id:
                values['site_id'] = args.site_id
            return _site_plan(service, kind, values)
        finally:
            service.close()
    if args.module == 'site':
        if args.operation == 'certificate' and args.action in {'issue', 'import'}:
            from .certificates import import_pair, issue
            if args.action == 'issue':
                return issue(args.domain, standalone=not args.webroot)
            return import_pair(args.certificate, args.key, args.domain)
        from .management import OperationService
        service = OperationService(synchronous=True)
        try:
            if args.action == 'list':
                return service.handle('web.resources', {})
            if args.operation == 'website':
                if args.action == 'create':
                    values = {'title': args.title, 'domain': args.domain, 'certificate_id': args.certificate_id}
                else:
                    values = {'site_id': args.site_id}
                    if args.action == 'domain':
                        values['domain'] = args.domain
                    elif args.action == 'certificate':
                        values['certificate_id'] = args.certificate_id
                kind = 'website.' + args.action
                if args.action in {'domain', 'certificate'}:
                    kind += '.' + args.resource_action
            else:
                if args.action in {'add', 'request'}:
                    values = {'name': args.name, 'domains': args.domain}
                else:
                    values = {'certificate_id': args.certificate_id}
                if args.action in {'add', 'replace'}:
                    values.update(certificate_pem=_read_pem(args.certificate), private_key_pem=_read_pem(args.key))
                kind = 'certificate.' + {'request': 'issue', 'replace': 'import'}.get(args.action, args.action)
            return _site_plan(service, kind, values)
        finally:
            service.close()
    if args.module == 'deployment' and args.operation == 'init':
        return _initial_config(args)
    if args.module == 'sub':
        config = json.loads(Path('/var/lib/proxyhub/current/config.json').read_text())
        port = config['server']['public_port']
        authority = config['server']['subscription_domain'] + ((':'+str(port)) if port != 443 else '')
        base = 'https://' + authority + '/sub/' + config['subscription']['token']
        result = {'mihomo': base+'/mihomo.yaml', 'raw': base+'/raw'}
        if args.output:
            atomic_write(args.output, canonical(result))
            return {'status': 'written', 'private_links': str(args.output)}
        return result
    from .state import StateStore
    store = StateStore(Path('/var/lib/proxyhub/plans'))
    if args.module == 'host':
        from .host.ssh import plan_append
        key = args.public_key_file.read_text(encoding='utf-8')
        if key.endswith('\n'):
            key = key[:-1]
            if key.endswith('\r'):
                key = key[:-1]
        plan = plan_append(args.account, key, connection_context={name:getattr(args,name) for name in ('addr','host','laddr','lport')}, selected_path=args.authorized_keys_file)
        identity = store.plan('ssh-append', plan)
        return {'plan_id':identity, 'account':args.account, 'target':args.authorized_keys_file,
                'fingerprint':plan.get('fingerprint'), 'action':'append existing public key', 'login_verification':'required'}
    if args.module == 'apply':
        kind, payload = store.get(args.plan_id)
        if kind == 'deployment':
            from .deployment import apply
            return apply(store,args.plan_id)
        if kind == 'ssh-append':
            from .host.ssh import apply_append
            with store.exclusive():
                _, payload = store.get(args.plan_id)
                result = apply_append(payload)
                store.commit(args.plan_id)
                return result
        if kind == 'panel-renew':
            from .management import OperationService
            service = OperationService(synchronous=True)
            try:
                return service.apply_plan(args.plan_id)
            finally:
                service.close()
        raise OperationError('Unsupported plan type')
    from .deployment import prepare, recover, apply
    if args.operation == 'plan':
        return prepare(store,json.loads(args.config.read_text()))
    if args.operation == 'recover':
        return recover(store)
    if args.operation == 'renew':
        from .certificates import issue, managed_acme
        base_revision = store.revision
        previous = Path('/var/lib/proxyhub/current').resolve()
        config = json.loads((previous/'config.json').read_text())
        if 'web' in config:
            from .certificates import acme_source
            from .management import OperationService
            from .management_errors import public_error
            from .websites import normalize_web
            bindings = [binding['id'] for binding in normalize_web(config)['certificates'] if acme_source(binding)]
            service = OperationService(synchronous=True)
            results = []
            recovery_required = False
            try:
                for identity in bindings:
                    with service.state() as current:
                        if current.pending():
                            results.append({'certificate_id':identity,'status':'failed','error':{'code':'BUSY'}})
                            recovery_required = True
                            break
                        revision = current.revision
                    try:
                        prepared = service.create_plan('certificate.renew',revision,{'certificate_id':identity})
                        results.append({'certificate_id':identity,**service.apply_plan(prepared['plan_id'])})
                    except Exception as exc:
                        error = public_error(exc)['code']
                        results.append({'certificate_id':identity,'status':'failed','error':{'code':error}})
                        with service.state() as current:
                            recovery_required = bool(current.pending()) or error == 'BUSY'
                        if recovery_required:
                            break
            finally:
                service.close()
            failed = sum(result.get('status') == 'failed' for result in results)
            result = {'status':('partial_failure' if failed < len(results) else 'failed') if failed else 'checked',
                      'certificates':results}
            if recovery_required:
                result['recovery_required'] = True
            return result
        server = config['server']
        if not managed_acme(server):
            return {'status':'skipped','certificate':'externally managed; renew through its original issuer'}
        certificate = issue([server['site_domain'],server['subscription_domain']], standalone=False)
        if store.revision != base_revision or Path('/var/lib/proxyhub/current').resolve() != previous:
            raise OperationError('Active configuration changed during certificate renewal; retry with the new revision')
        if certificate['certificate_path'] == server['certificate_path']:
            return {'status':'unchanged','certificate':'not due for renewal'}
        server.update(certificate_path=certificate['certificate_path'],key_path=certificate['key_path'])
        plan = prepare(store,config,expected_revision=base_revision)
        return apply(store,plan['plan_id'])
    raise OperationError('Unsupported command')


def main(argv=None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.module == 'help':
        _help(root,args.topic)
        return 0
    if args.module is None:
        root.print_help()
        return 0
    if args.module in {'host','site','proxy','deployment','sub','panel'} and args.operation is None:
        _help(root,[args.module])
        return 0
    if args.module == 'site' and args.action is None:
        _help(root,['site',args.operation])
        return 0
    if args.module == 'proxy' and args.action is None:
        _help(root,['proxy',args.operation])
        return 0
    if args.module == 'site' and args.operation == 'website' and args.action in {'domain', 'certificate'} and args.resource_action is None:
        _help(root, ['site', 'website', args.action])
        return 0
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.module == 'deployment' and args.operation == 'renew' and result.get('status') in {'partial_failure','failed'}:
            return 1
        return 0
    except Exception as exc:
        # Never echo runtime output, config, key, token, or exception paths.
        print('ProxyHub operation failed ('+type(exc).__name__+'); inspect the private plan and service status.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
