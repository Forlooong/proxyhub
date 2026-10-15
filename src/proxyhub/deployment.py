"""Single-node immutable candidates, explicit activation and rollback."""
from __future__ import annotations

import json
import http.client
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import stat
import time

from .core import OperationError, atomic_write, canonical, digest, run
from .proxy import validate_spec, render_sing_box
from .site import render_nginx, render_site, validate_certificate
from .publication import build_bundle, save_bundle, validate_bundle, MAX_BODY_BYTES
from .state import StateStore
from .websites import normalize_web, get_site, get_certificate, consumers, site_directory, sync_default
from . import reality

ROOT = Path('/var/lib/proxyhub')
BIN = Path('/opt/proxyhub/bin')
APP = Path('/opt/proxyhub/app')
CANDIDATE_FILES = ('sing-box.json', 'nginx.conf', 'site/index.html', 'bundle.json', 'config.json', 'client.yaml')


def _read_site(directory: Path, manifest: dict, site_id: str = 'default') -> dict[str, bytes]:
    """Read only the bounded static tree recorded in an immutable plan."""
    from .site_content import MAX_EXPANDED_BYTES, MAX_FILES, validate_files
    prefix = site_directory(site_id)
    site = directory / prefix
    if (site.is_symlink() or not site.is_dir()
            or (site_id != 'default' and (directory/'sites').is_symlink())):
        raise OperationError('Website content is not a managed directory')
    files, total, entries = {}, 0, 0
    for parent, directories, names in os.walk(site, followlinks=False):
        for name in directories + names:
            entries += 1
            path = Path(parent) / name
            if entries > MAX_FILES or path.is_symlink():
                raise OperationError('Website content contains unsafe or excessive entries')
            if name in directories:
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise OperationError('Website content is not a regular file')
            relative = path.relative_to(site).as_posix()
            if prefix + '/' + relative not in manifest:
                raise OperationError('Website content differs from its immutable manifest')
            with path.open('rb') as source:
                body = source.read(MAX_EXPANDED_BYTES - total + 1)
            total += len(body)
            if total > MAX_EXPANDED_BYTES:
                raise OperationError('Website content exceeds the size limit')
            files[relative] = body
    files = validate_files(files)
    if {prefix + '/' + name: digest(body) for name, body in files.items()} != manifest:
        raise OperationError('Website content changed after validation')
    return files


def _site_manifest(payload: dict, site_id: str) -> dict:
    prefix = site_directory(site_id) + '/'
    return {name: value for name, value in payload['files'].items() if name.startswith(prefix)}


def _content_source(payload: dict, site_id: str):
    if 'content_versions' in payload:
        return payload['content_versions'].get(site_id)
    return payload.get('content_version') if site_id == 'default' else None


def content_version(store: StateStore, version: str, site_id: str = 'default') -> dict[str, bytes]:
    """Only successfully applied uploads are eligible for content restoration."""
    if not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{32}', version):
        raise OperationError('Invalid website content version')
    for (identity,) in store.db.execute("SELECT id FROM plans WHERE kind='deployment' AND status='applied' ORDER BY created DESC, rowid DESC"):
        payload = store.inspect(identity)['payload']
        if payload.get('revision') == version and _content_source(payload,site_id) == version:
            directory = _managed_revision(ROOT / 'revisions' / version)
            return _read_site(directory,_site_manifest(payload,site_id),site_id)
    raise OperationError('Website content version has not been successfully applied')


def content_versions(store: StateStore, config: dict, site_id: str = 'default') -> dict:
    site = get_site(normalize_web(config),site_id)
    current = site['content_version']
    template = render_site({**config['server'],'site_title':site['title']}).encode()
    versions = [{'id': 'template', 'source': 'template', 'created': None,
                 'file_count': 1, 'total_bytes': len(template), 'current': current is None}]
    seen = set()
    for identity, created in store.db.execute("SELECT id,created FROM plans WHERE kind='deployment' AND status='applied' ORDER BY created DESC, rowid DESC"):
        payload = store.inspect(identity)['payload']
        version = _content_source(payload,site_id)
        if version is None or version != payload.get('revision') or version in seen:
            continue
        seen.add(version)
        if len(versions) <= 50 or version == current:
            summary = (payload.get('website_summaries',{}).get(site_id)
                       or (payload.get('website_summary') if site_id == 'default' else None))
            versions.append({'id': version, 'source': 'upload', 'created': created,
                             **summary, 'current': current == version})
    return {'current_version': current, 'versions': versions}


def _verify_candidate(directory: Path, payload: dict) -> None:
    manifest = payload['files']
    if not isinstance(manifest, dict) or not set(CANDIDATE_FILES) <= set(manifest):
        raise OperationError('Candidate file manifest is invalid')
    for name in set(CANDIDATE_FILES) - {'site/index.html'}:
        file = directory / name
        if file.is_symlink() or not file.is_file() or digest(file.read_bytes()) != manifest[name]:
            raise OperationError('Candidate file changed after validation')
    config = json.loads((directory/'config.json').read_text(encoding='utf-8'))
    sites = normalize_web(config)['sites'] if 'web' in config else [{'id':'default'}]
    allowed = set(CANDIDATE_FILES) - {'site/index.html'}
    for site in sites:
        part = _site_manifest(payload,site['id'])
        _read_site(directory,part,site['id'])
        allowed.update(part)
    expected = {'reality/'+entry['id']+'.json' for entry in reality.normalize_reality(config)['endpoints']
                if entry['id'] != 'default'} if 'reality' in config else set()
    for name in expected:
        file = directory/name
        if (directory/'reality').is_symlink() or file.is_symlink() or not file.is_file() or digest(file.read_bytes()) != manifest.get(name):
            raise OperationError('REALITY candidate file changed after validation')
    if set(manifest) != allowed | expected:
        raise OperationError('Candidate file manifest contains an unmanaged site or REALITY entry')
    runtime = directory/'reality'
    if runtime.exists() or runtime.is_symlink():
        if runtime.is_symlink() or not runtime.is_dir() or {path.relative_to(directory).as_posix() for path in runtime.iterdir()} != expected:
            raise OperationError('Candidate contains an unmanaged REALITY configuration')
    parent = directory/'sites'
    if parent.exists() or parent.is_symlink():
        if parent.is_symlink() or not parent.is_dir():
            raise OperationError('Unsafe additional website directory')
        expected = {site['id'] for site in sites if site['id'] != 'default'}
        if {path.name for path in parent.iterdir()} != expected:
            raise OperationError('Candidate contains an unmanaged website tree')


def client_config(server: dict, overrides: dict, sni: str | None = None) -> dict:
    node = {key: server[key] for key in ('uuid', 'public_key', 'short_id')}
    node.update(id='local-reality', name=server['node_name'], server=server['public_address'],
                port=server['public_port'], server_name=sni or server['site_domain'])
    text = run([str(BIN/'node'), str(APP/'packages/converter/dist/worker.js')], input=json.dumps({'node':node,'overrides':overrides}), timeout=20)
    return json.loads(text)


def _certificate_bindings(config: dict) -> dict:
    web = normalize_web(config)
    result = {}
    for binding in web['certificates']:
        active = [item['domain'] for item in consumers(web,config['server'],binding['id'],config.get('reality'))
                  if item.get('enabled',True) and item['domain']]
        details = validate_certificate(binding['certificate_path'],binding['key_path'],
                                       list(dict.fromkeys(active))) if active else {'sha256':None}
        result[binding['id']] = {'sha256':details['sha256'],'files':_certificate_files(binding)}
    return result


def _preserve_endpoints(config: dict, previous: Path | None, *, reality_operation: dict | None = None) -> None:
    if previous is None:
        return
    old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
    if (reality_operation is None and ('reality' in config or 'reality' in old)
            and reality.normalize_reality(config) != reality.normalize_reality(old)):
        raise OperationError('REALITY targets and entries require an explicit migration, selection or retirement plan')
    fields = ('site_domain','subscription_domain','http_port','public_port','reality_port',
              'site_tls_port','subscription_tls_port','publisher_port')
    # The normalized server shape owns the complete listener contract.
    if all(field in old['server'] and field in config['server'] for field in fields):
        if any(old['server'][field] != config['server'][field] for field in fields):
            raise OperationError('Existing SNI and listener endpoints require the separate migration stage')
    if 'web' in old and 'web' in config:
        old_web, new_web = normalize_web(old), normalize_web(config)
        before = {site['id']:site for site in old_web['sites']}
        after = {site['id']:site for site in new_web['sites']}
        if any(site['enabled'] for identity,site in before.items() if identity not in after):
            raise OperationError('An enabled website must be disabled before deletion')
        for binding in old_web['certificates']:
            if (not any(item['id']==binding['id'] for item in new_web['certificates'])
                    and consumers(old_web,old['server'],binding['id'],old.get('reality'))):
                raise OperationError('A referenced certificate binding cannot be deleted')
        for site in new_web['sites']:
            previous_site = before.get(site['id'])
            if previous_site and any(previous_site[key] != site[key] for key in ('listener_id','tls_port')):
                raise OperationError('An existing website listener identity and port cannot change')


def _reality_transition(config: dict, previous: Path | None, operation: dict) -> None:
    """Only the approved resource delta can unlock the migration boundary."""
    if previous is None or not isinstance(operation,dict) or set(operation)!={'kind','endpoint_id'}:
        raise OperationError('REALITY activation requires an existing deployment and a typed transition')
    old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
    old = sync_default(old)
    old['server'] = validate_spec(old['server'])
    if ({key:value for key,value in config.items() if key!='reality'}
            != {key:value for key,value in old.items() if key!='reality'}):
        raise OperationError('REALITY activation cannot change websites, node credentials or subscription settings')
    before, after = reality.normalize_reality(old), reality.normalize_reality(config)
    old_entries = {entry['id']:entry for entry in before['endpoints']}
    new_entries = {entry['id']:entry for entry in after['endpoints']}
    old_targets = {target['id']:target for target in before['targets']}
    new_targets = {target['id']:target for target in after['targets']}
    identity, kind = operation['endpoint_id'], operation['kind']
    if kind == 'migrate':
        entry = reality.get_endpoint(after,identity)
        valid = (identity not in old_entries and set(new_entries)==set(old_entries)|{identity}
                 and all(new_entries[key]==value for key,value in old_entries.items())
                 and entry['target_id'] not in old_targets
                 and set(new_targets)==set(old_targets)|{entry['target_id']}
                 and all(new_targets[key]==value for key,value in old_targets.items())
                 and after['primary_endpoint_id']==identity)
    elif kind == 'select':
        valid = (identity in old_entries and old_entries==new_entries and old_targets==new_targets
                 and after['primary_endpoint_id']==identity)
    elif kind == 'retire':
        entry = reality.get_endpoint(before,identity)
        valid = (identity!=before['primary_endpoint_id'] and identity not in new_entries
                 and new_entries=={key:value for key,value in old_entries.items() if key!=identity}
                 and new_targets=={key:value for key,value in old_targets.items() if key!=entry['target_id']}
                 and after['primary_endpoint_id']==before['primary_endpoint_id'])
    else:
        valid = False
    if not valid:
        raise OperationError('REALITY transition differs from its approved resource operation')
    _preserve_endpoints(config,previous,reality_operation=operation)


def _render_reality(config: dict, entry: dict) -> dict:
    rendered = render_sing_box(config['server'])
    inbound = rendered['inbounds'][0]
    target = reality.endpoint_target(config,entry)
    inbound.update(tag='proxyhub-reality-'+entry['id'],listen_port=entry['listen_port'])
    inbound['tls']['server_name'] = target['server_name']
    inbound['tls']['reality']['handshake'] = {key:target[key] for key in ('server','server_port')}
    return rendered


def _reality_scope(config: dict, scope: str) -> dict | None:
    """Probe changed paths without making recovery depend on an old remote outage."""
    resources = reality.normalize_reality(config)
    targets = {target['id']:target for target in resources['targets']}
    entries = [entry for entry in resources['endpoints'] if
               (entry['id']==resources['primary_endpoint_id'] if scope=='primary'
                else targets[entry['target_id']]['type']=='site')]
    if not entries:
        return None
    identities = {entry['id'] for entry in entries}
    primary = resources['primary_endpoint_id']
    return {**config,'reality':{'endpoints':entries,
            'targets':[targets[entry['target_id']] for entry in entries],
            'primary_endpoint_id':primary if primary in identities else entries[0]['id']}}


def _web_server(config: dict, previous: Path | None) -> None:
    if previous is None:
        raise OperationError('Web-only activation requires an active deployment')
    old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
    allowed = {'site_title','certificate_path','key_path'}
    if ({key:value for key,value in validate_spec(config['server']).items() if key not in allowed}
            != {key:value for key,value in validate_spec(old['server']).items() if key not in allowed}
            or any(config[key] != old[key] for key in ('subscription','overrides'))):
        raise OperationError('Web-only activation cannot change proxy or subscription configuration')
    _preserve_endpoints(config,previous)


def prepare(store: StateStore, config: dict, *, expected_revision: int | None = None,
            publish_only: bool = False, site_files: dict[str, bytes] | None = None,
            website_change: bool = False, site_id: str = 'default', web_only: bool = False,
            reality_operation: dict | None = None) -> dict:
    base_revision = store.revision if expected_revision is None else expected_revision
    if store.revision != base_revision:
        raise OperationError('Configuration changed before preparation')
    if store.pending():
        raise OperationError('Recover the interrupted activation before preparing a deployment')
    if (not {'server','subscription','overrides'} <= set(config)
            or set(config) - {'server','subscription','overrides','website_content','web','reality'}):
        raise OperationError('Deployment contains unknown or missing objects')
    if (type(publish_only) is not bool or type(web_only) is not bool
            or sum((publish_only,web_only,reality_operation is not None))>1):
        raise OperationError('Activation mode selection must be unambiguous')
    original = config
    config = sync_default(config)
    if 'web' in original and (original.get('website_content') != config.get('website_content')
            or any(original['server'].get(key) != config['server'].get(key)
                   for key in ('site_title','certificate_path','key_path'))):
        raise OperationError('Legacy fields conflict with the managed default website; update the website resource')
    server = validate_spec(config['server'])
    config['server'] = server
    if server['publisher_port'] != 18890:
        raise OperationError('The publisher unit reserves loopback port 18890')
    previous = _current_revision()
    if publish_only:
        _publication_server(server,previous,config)
        if site_files is not None:
            raise OperationError('Publication-only activation cannot change website content')
    if reality_operation is not None:
        _reality_transition(config,previous,reality_operation)
    elif web_only:
        _web_server(config,previous)
    elif not publish_only:
        _preserve_endpoints(config,previous)
    if not publish_only:
        _check_new_domains(config,previous)
    if reality_operation is not None:
        reality.probe_targets(_reality_scope(config,'primary'))
    bindings = _certificate_bindings(config)
    if 'reality' in config:
        primary = next(entry for entry in reality.public_resources(config)['endpoints'] if entry['primary'])
        generated = client_config(server,config['overrides'],primary['sni'])
    else:
        generated = client_config(server,config['overrides'])
    revision = secrets.token_hex(16)
    selected = get_site(config['web'],site_id)
    from .site_content import validate_files
    if site_files is not None:
        site_files = validate_files(site_files)
        selected['content_version'] = revision
        config = sync_default(config)
    directory = ROOT/'revisions'/revision
    directory.mkdir(parents=True,mode=0o711)
    try:
        directory.chmod(0o711)
        import pwd
        proxy_gid = pwd.getpwnam('proxyhub-proxy').pw_gid
        singbox = directory/'sing-box.json'
        atomic_write(singbox,canonical(render_sing_box(server)),0o640)
        os.chown(singbox,0,proxy_gid)
        if 'reality' in config:
            for entry in reality.normalize_reality(config)['endpoints']:
                if entry['id']=='default':
                    continue
                path = directory/'reality'/(entry['id']+'.json')
                atomic_write(path,canonical(_render_reality(config,entry)),0o640)
                path.parent.chmod(0o711)
                os.chown(path,0,proxy_gid)
                run([str(BIN/'sing-box'),'check','-c',str(path)])
        nginx = (render_nginx(server,str(directory),config['web'],config['reality']) if 'reality' in config
                 else render_nginx(server,str(directory),config['web']))
        atomic_write(directory/'nginx.conf',nginx.encode(),0o600)
        summaries = {}
        for site in config['web']['sites']:
            identity = site['id']
            if identity == site_id and site_files is not None:
                content = site_files
                summaries[identity] = {'file_count':len(content),'total_bytes':sum(map(len,content.values()))}
            elif site['content_version']:
                content = content_version(store,site['content_version'],identity)
            else:
                content = {'index.html':render_site({**server,'site_title':site['title']}).encode()}
            for name,body in content.items():
                target = directory/site_directory(identity)/name
                atomic_write(target,body,0o644)
                parent = target.parent
                while parent != directory:
                    parent.chmod(0o755)
                    parent = parent.parent
        subscription = dict(config['subscription'])
        subscription.update(host=server['subscription_domain'],yaml=generated['yaml'],raw=generated['raw'])
        save_bundle(directory/'bundle.json',build_bundle([subscription],revision))
        atomic_write(directory/'config.json',canonical(config))
        atomic_write(directory/'client.yaml',generated['yaml'].encode())
        run([str(BIN/'sing-box'),'check','-c',str(singbox)])
        run([str(BIN/'nginx'),'-e','stderr','-p',str(ROOT/'nginx')+'/','-t','-c',str(directory/'nginx.conf')])
        run([str(BIN/'mihomo'),'-t','-d',str(ROOT/'client-check'),'-f',str(directory/'client.yaml')])
        _check_new_ports(config,previous)
        if 'reality' in config:
            _check_reality_ports(config,previous)
        files = {path.relative_to(directory).as_posix():digest(path.read_bytes())
                 for path in directory.rglob('*') if path.is_file()}
        default_binding = get_site(config['web'],'default')['certificate_id']
        payload = {'revision':revision,'files':files,'site_domain':server['site_domain'],
                   'subscription_domain':server['subscription_domain'],
                   'certificate_sha256':bindings[default_binding]['sha256'],
                   'certificate_files':_certificate_files(server),'certificate_bindings':bindings,
                   'content_versions':{site['id']:site['content_version'] for site in config['web']['sites']
                                       if site['content_version']},
                   'website_summaries':summaries,
                   'activation':('reality' if reality_operation is not None else
                                 'publication' if publish_only else 'web' if web_only else 'full')}
        if reality_operation is not None:
            payload['reality_operation'] = reality_operation
        if config.get('website_content'):
            payload['content_version'] = config['website_content']
        if 'default' in summaries:
            payload['website_summary'] = summaries['default']
        if website_change:
            payload.update(website_change=True,website_id=site_id)
        plan_id = store.plan('deployment',payload,expected_revision=base_revision)
        changes = (['subscription bundle','publisher reload'] if publish_only else
                   ['new or retained REALITY entries','Nginx reload','subscription SNI publication'] if reality_operation is not None else
                   ['website resources','Nginx reload','publisher reload'] if web_only else
                   ['managed websites','REALITY listener','subscription bundle','ProxyHub service activation'])
        return {'plan_id':plan_id,'revision':revision,'site_domain':server['site_domain'],
                'subscription_domain':server['subscription_domain'],'checks':['certificates','sing-box','nginx','mihomo'],
                'activation':payload['activation'],'changes':changes}
    except Exception:
        shutil.rmtree(directory)
        raise



def _activation(payload: dict) -> str:
    """Missing mode identifies a phase-1 full-deployment record."""
    mode = payload.get('activation', 'full')
    if mode not in ('full', 'publication', 'web', 'reality'):
        raise OperationError('Unsupported activation mode')
    return mode


def _publication_server(server: dict, previous: Path | None, config: dict | None = None) -> None:
    """Publication-only plans must not silently change any server-owned field."""
    if previous is None:
        raise OperationError('Publication-only activation requires an active deployment')
    path = previous / 'config.json'
    if path.is_symlink() or not path.is_file():
        raise OperationError('Active deployment configuration is not a managed file')
    current = json.loads(path.read_text(encoding='utf-8'))
    if validate_spec(server) != validate_spec(current['server']):
        raise OperationError('Publication-only activation cannot change server configuration')
    if config is not None and config.get('website_content') != current.get('website_content'):
        raise OperationError('Publication-only activation cannot change website content')
    if config is not None and normalize_web(config) != normalize_web(current):
        raise OperationError('Publication-only activation cannot change website resources')
    if (config is not None and ('reality' in config or 'reality' in current)
            and reality.normalize_reality(config) != reality.normalize_reality(current)):
        raise OperationError('Publication-only activation cannot change REALITY targets or entries')


def _link(directory: Path) -> None:
    temp = ROOT/('.current-'+secrets.token_hex(8))
    temp.symlink_to(directory)
    os.replace(temp, ROOT/'current')
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(directory: Path) -> None:
    import pwd
    target = ROOT/'publish/bundle.json'
    save_bundle(target, json.loads((directory/'bundle.json').read_text(encoding='utf-8')))
    os.chown(target, 0, pwd.getpwnam('proxyhub-pub').pw_gid)
    target.chmod(0o640)


def _units() -> tuple[str, ...]:
    from .installer import UNITS
    return UNITS


def _reality_units(config: dict) -> dict[str,str]:
    return {entry['id']:('proxyhub-proxy.service' if entry['id']=='default' else
                         'proxyhub-reality@'+entry['id']+'.service')
            for entry in reality.normalize_reality(config)['endpoints']}


def _core_units(config: dict) -> tuple[str,...]:
    if 'reality' not in config:
        return _units()
    entries = _reality_units(config)
    return (*entries.values(),*(unit for unit in _units() if unit!='proxyhub-proxy.service'))


def _managed_revision(path: str | Path) -> Path:
    directory = Path(path)
    revisions = ROOT / 'revisions'
    if (not directory.is_absolute() or directory.parent != revisions
            or not re.fullmatch(r'[0-9a-f]{32}', directory.name)
            or ROOT.is_symlink() or revisions.is_symlink()
            or directory.is_symlink() or not directory.is_dir()):
        raise OperationError('Revision is not a managed immutable directory')
    return directory


def _current_revision() -> Path | None:
    current = ROOT / 'current'
    if current.is_symlink():
        # readlink avoids silently accepting a link that traverses outside the
        # managed tree and then re-enters it through another symlink.
        return _managed_revision(current.readlink())
    if current.exists():
        raise OperationError('Current revision path is not a managed link')
    return None


def _sync_root() -> None:
    if os.name == 'posix':
        fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _clear_current() -> None:
    current = ROOT / 'current'
    if current.is_symlink():
        current.unlink()
        _sync_root()
    elif current.exists():
        raise OperationError('Current revision path is not a managed link')


def _certificate_files(server: dict) -> dict:
    result = {}
    for key in ('certificate_path', 'key_path'):
        path = Path(server[key])
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise OperationError('Certificate must be a regular managed file')
        result[key] = digest(path.read_bytes())
    return result


def _check_runtime_permissions(directory: Path) -> None:
    """Reject unusable candidates before switching links or touching services.

    Check publication candidates too: current becomes the next startup input
    for proxy/Nginx even when this activation reloads only the publisher.
    """
    if os.name != 'posix':
        return
    config = json.loads((directory/'config.json').read_text(encoding='utf-8'))
    identities = [site['id'] for site in normalize_web(config)['sites']] if 'web' in config else ['default']
    trees = [directory/site_directory(identity) for identity in identities]
    parents = [directory/'sites'] if len(trees)>1 else []
    extra = list((directory/'reality').glob('*.json')) if 'reality' in config else []
    if extra:
        parents.append(directory/'reality')
    for path in (ROOT, ROOT/'revisions', directory, *parents, *trees,
                 *(path for tree in trees for path in tree.rglob('*') if path.is_dir())):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or not info.st_mode & stat.S_IXOTH
                or info.st_mode & 0o022):
            raise OperationError('Managed runtime directory permissions do not allow safe service access')
    for path, required in ((directory/'sing-box.json', stat.S_IRGRP),
                           *((path,stat.S_IRGRP) for path in extra),
                           *((path, stat.S_IROTH) for tree in trees for path in tree.rglob('*') if path.is_file())):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or not info.st_mode & required
                or info.st_mode & 0o022):
            raise OperationError('Managed runtime file permissions do not allow safe service access')


def _check_ports(server: dict) -> None:
    for port in (server['http_port'], server['public_port'], server['reality_port'],
                 server['site_tls_port'], server['subscription_tls_port'], server['publisher_port']):
        for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
            try:
                with socket.socket(family) as test:
                    if family == socket.AF_INET6:
                        test.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    test.bind((address, port))
            except OSError as exc:
                raise OperationError(f'TCP port {port} is unavailable; no service was replaced') from exc


def _check_new_ports(config: dict, previous: Path | None) -> None:
    web = normalize_web(config)
    owned = {config['server']['site_tls_port']}
    if previous is not None:
        old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
        owned.update(site['tls_port'] for site in normalize_web(old)['sites'] if site['enabled'])
    for site in web['sites']:
        port = site['tls_port']
        if not site['enabled'] or port in owned:
            continue
        for family,address in ((socket.AF_INET,'0.0.0.0'),(socket.AF_INET6,'::')):
            try:
                with socket.socket(family) as probe:
                    if family == socket.AF_INET6:
                        probe.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
                    probe.bind((address,port))
            except OSError:
                raise OperationError(f'New website TCP port {port} is unavailable; no service was replaced') from None


def _check_new_domains(config: dict, previous: Path | None) -> None:
    from .management_web import check_domain
    old_domains = set()
    if previous is not None:
        old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
        old_domains = {domain for site in normalize_web(old)['sites'] if site['enabled'] for domain in site['domains']}
    domains = {domain for site in normalize_web(config)['sites'] if site['enabled'] for domain in site['domains']}
    for domain in sorted(domains-old_domains):
        check_domain(config['server'],domain)


def _check_reality_ports(config: dict, previous: Path | None) -> None:
    old = (reality.normalize_reality(json.loads((previous/'config.json').read_text(encoding='utf-8')))
           if previous is not None else {'endpoints':[]})
    owned = {entry['listen_port'] for entry in old['endpoints']}
    for entry in reality.normalize_reality(config)['endpoints']:
        port = entry['listen_port']
        if port in owned:
            continue
        for family,address in ((socket.AF_INET,'0.0.0.0'),(socket.AF_INET6,'::')):
            try:
                with socket.socket(family) as checked:
                    if family==socket.AF_INET6:
                        checked.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
                    checked.bind((address,port))
            except OSError:
                raise OperationError(f'New REALITY TCP port {port} is unavailable; no service was replaced') from None


def _restore(previous: Path | None, candidate: Path, activation: str = 'full',
             reality_operation: dict | None = None) -> None:
    current = _current_revision()
    if current not in (None, previous, candidate):
        raise OperationError('Refusing to overwrite an unexpected current revision')
    if activation == 'publication':
        # Re-check this invariant during crash recovery as well as normal apply.
        # A publication rollback must never mask a server configuration change.
        config = json.loads((candidate / 'config.json').read_text(encoding='utf-8'))
        _publication_server(config['server'], previous, config)
        _link(previous)
        _publish(previous)
        run(['systemctl', 'reload', 'proxyhub-publisher.service'])
        _wait_publication(previous)
        return
    if activation == 'web':
        config = json.loads((candidate/'config.json').read_text(encoding='utf-8'))
        _web_server(config,previous)
        restored = json.loads((previous/'config.json').read_text(encoding='utf-8'))
        _link(previous)
        _publish(previous)
        run(['systemctl','reload','proxyhub-nginx.service'])
        run(['systemctl','reload','proxyhub-publisher.service'])
        _wait_web_health(restored,previous)
        if 'reality' in restored:
            _wait_local_reality_health(restored,previous)
        _wait_publication(previous)
        return
    if activation == 'reality':
        config = json.loads((candidate/'config.json').read_text(encoding='utf-8'))
        _reality_transition(config,previous,reality_operation)
        restored = json.loads((previous/'config.json').read_text(encoding='utf-8'))
        _link(previous)
        _publish(previous)
        old_units, new_units = _reality_units(restored), _reality_units(config)
        for unit in old_units.values():
            run(['systemctl','start',unit])
        run(['systemctl','reload','proxyhub-nginx.service'])
        run(['systemctl','reload','proxyhub-publisher.service'])
        _wait_web_health(restored,previous)
        _wait_local_reality_health(restored,previous)
        _wait_publication(previous)
        for identity,unit in new_units.items():
            if identity not in old_units:
                run(['systemctl','stop',unit])
                run(['systemctl','disable',unit])
        for unit in old_units.values():
            run(['systemctl','enable',unit])
        return
    if activation != 'full':
        raise OperationError('Unsupported activation mode')
    if previous is not None:
        config = json.loads((previous / 'config.json').read_text(encoding='utf-8'))
        _link(previous)
        _publish(previous)
        for service in _core_units(config):
            run(['systemctl', 'restart', service])
        # systemctl restart acknowledges process startup, not HTTPS readiness.
        # Keep the activation pending until every restored consumer is healthy.
        if 'reality' not in config:
            _wait_health(config['server'])
        if 'web' in config:
            _wait_web_health(config,previous)
        if 'reality' in config:
            _wait_local_reality_health(config,previous)
    else:
        # Every service is attempted even if one stop fails. The journal is left
        # pending when any action fails, so a later recover can safely retry.
        errors = []
        for service in reversed(_units()):
            try:
                run(['systemctl', 'stop', service])
            except Exception as exc:
                errors.append(exc)
        try:
            run(['systemctl', 'disable', *_units()])
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise OperationError('Service rollback incomplete; run deployment recover') from errors[0]
        _clear_current()
        bundle = ROOT / 'publish/bundle.json'
        if bundle.is_symlink():
            raise OperationError('Unsafe publication path during rollback')
        bundle.unlink(missing_ok=True)
        if os.name == 'posix':
            fd = os.open(bundle.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def recover(store: StateStore) -> dict:
    """Conservatively undo an interrupted activation; never continue it."""
    with store.exclusive():
        pending = store.pending()
        if not pending:
            return {'status': 'nothing_to_recover'}
        if len(pending) != 1:
            raise OperationError('Multiple pending activations require manual investigation')
        item = pending[0]
        record = store.inspect(item['plan_id'])
        if record['kind'] != 'deployment' or record['status'] != 'prepared':
            raise OperationError('Pending activation has an inconsistent plan state')
        details = item['details']
        activation = _activation(record['payload'])
        if _activation(details) != activation:
            raise OperationError('Recovery activation mode differs from its plan')
        candidate = _managed_revision(details['candidate'])
        if candidate.name != record['payload']['revision']:
            raise OperationError('Recovery candidate differs from its plan')
        previous = _managed_revision(details['previous']) if details['previous'] else None
        if activation=='reality':
            _verify_candidate(candidate,record['payload'])
            _restore(previous,candidate,activation,record['payload'].get('reality_operation'))
        else:
            _restore(previous, candidate, activation)
        store.abort(item['plan_id'], {'recovered': True, 'previous_available': previous is not None})
        return {'status': 'recovered', 'previous_available': previous is not None}


def _health(server: dict) -> None:
    context = ssl.create_default_context()
    for domain, port in ((server['site_domain'],server['site_tls_port']),
                         (server['subscription_domain'],server['subscription_tls_port'])):
        with socket.create_connection(('127.0.0.1',port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=domain) as stream:
                stream.sendall(f'GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n'.encode())
                status = stream.recv(2048)
                expected = b'200' if domain == server['site_domain'] else b'404'
                if expected not in status.split(b'\r\n',1)[0]:
                    raise OperationError('Website or subscription HTTPS health check failed')
    with socket.create_connection(('127.0.0.1',server['reality_port']),timeout=5):
        pass


def _wait_health(server: dict) -> None:
    """Bound startup retries for both activation and restoration."""
    for attempt in range(15):
        try:
            _health(server)
            return
        except (OSError, OperationError):
            if attempt == 14:
                raise
            time.sleep(0.2)


def _website_health(server: dict, directory: Path) -> None:
    """A running web server must actually serve the selected index before commit."""
    expected = (directory / 'site/index.html').read_bytes()
    with socket.create_connection(('127.0.0.1', server['site_tls_port']), timeout=5) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=server['site_domain']) as stream:
            stream.sendall(f"GET / HTTP/1.1\r\nHost: {server['site_domain']}\r\nConnection: close\r\n\r\n".encode())
            response = http.client.HTTPResponse(stream)
            try:
                response.begin()
                if response.status != 200 or response.read(len(expected) + 1) != expected:
                    raise OperationError('Website has not loaded the selected content')
            finally:
                response.close()


def _web_health(config: dict, directory: Path) -> None:
    """Check every active TLS consumer against its actual certificate and content."""
    web = normalize_web(config)
    bindings = _certificate_bindings(config)
    targets = []
    for site in web['sites']:
        if site['enabled']:
            body = (directory/site_directory(site['id'])/'index.html').read_bytes()
            for domain in site['domains']:
                targets.append((domain,site['tls_port'],site['certificate_id'],body,200))
    targets.append((config['server']['subscription_domain'],config['server']['subscription_tls_port'],
                    web['subscription_certificate_id'],None,404))
    context = ssl.create_default_context()
    for domain,port,identity,body,status in targets:
        with socket.create_connection(('127.0.0.1',port),timeout=2) as raw:
            with context.wrap_socket(raw,server_hostname=domain) as stream:
                if digest(stream.getpeercert(binary_form=True)) != bindings[identity]['sha256']:
                    raise OperationError('A TLS consumer has not loaded the selected certificate')
                stream.sendall(f'GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n'.encode())
                response = http.client.HTTPResponse(stream)
                try:
                    response.begin()
                    if response.status != status or (body is not None and response.read(len(body)+1) != body):
                        raise OperationError('A website consumer has not loaded the selected content')
                finally:
                    response.close()


def _wait_web_health(config: dict, directory: Path) -> None:
    for attempt in range(20):
        try:
            _web_health(config,directory)
            return
        except (OSError,OperationError,http.client.HTTPException):
            if attempt == 19:
                raise
            time.sleep(.2)


def _reality_health(config: dict, directory: Path) -> None:
    """Follow every retained SNI through the actual public stream mapping."""
    web = normalize_web(config)
    context = ssl.create_default_context()
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    for entry in reality.normalize_reality(config)['endpoints']:
        target = reality.endpoint_target(config,entry)
        expected, fingerprint = None, None
        if 'site_id' in target:
            site = get_site(web,target['site_id'])
            binding = get_certificate(web,site['certificate_id'])
            fingerprint = validate_certificate(binding['certificate_path'],binding['key_path'],
                                               [target['server_name']])['sha256']
            expected = (directory/site_directory(site['id'])/'index.html').read_bytes()
        with socket.create_connection(('127.0.0.1',config['server']['public_port']),timeout=3) as raw:
            with context.wrap_socket(raw,server_hostname=target['server_name']) as stream:
                if fingerprint is not None and digest(stream.getpeercert(binary_form=True))!=fingerprint:
                    raise OperationError('A REALITY SNI route has not loaded the selected target certificate')
                if expected is not None:
                    stream.sendall(f"GET / HTTP/1.1\r\nHost: {target['server_name']}\r\nConnection: close\r\n\r\n".encode())
                    response = http.client.HTTPResponse(stream)
                    try:
                        response.begin()
                        if response.status!=200 or response.read(len(expected)+1)!=expected:
                            raise OperationError('A REALITY SNI route has not loaded the selected website')
                    finally:
                        response.close()


def _wait_reality_health(config: dict, directory: Path) -> None:
    for attempt in range(15):
        try:
            _reality_health(config,directory)
            return
        except (OSError,OperationError,http.client.HTTPException):
            if attempt==14:
                raise
            time.sleep(.2)


def _wait_local_reality_health(config: dict, directory: Path) -> None:
    local = _reality_scope(config,'local')
    if local is not None:
        _wait_reality_health(local,directory)


def _publication_health(directory: Path) -> None:
    """Check the loaded generation, exact bodies and headers through real GETs.

    A successful SIGHUP only schedules a reload. The publisher deliberately
    retains the previous valid snapshot on failure, so process health alone is
    not an acknowledgement. Requests stay on the fixed loopback publisher port;
    no URL from a browser or subscription configuration is fetched here.
    """
    bundle = validate_bundle(json.loads((directory / 'bundle.json').read_text(encoding='utf-8')))
    if bundle['generation'] != directory.name or len(bundle['profiles']) != 1:
        raise OperationError('Publication acknowledgement requires the managed single subscription')
    profile = bundle['profiles'][0]
    for expected in profile['responses'].values():
        connection = http.client.HTTPConnection('127.0.0.1', 18890, timeout=1)
        try:
            connection.request('GET', expected['path'], headers={
                'Host': profile['host'], 'Connection': 'close',
            })
            response = connection.getresponse()
            generation = response.headers.get_all('X-ProxyHub-Generation', [])
            if response.status != 200 or generation != [bundle['generation']]:
                raise OperationError('Publisher has not acknowledged the expected generation')
            body = expected['body'].encode('utf-8')
            if response.headers.get_all('Content-Length', []) != [str(len(body))]:
                raise OperationError('Publisher response length differs from the candidate')
            if any(response.headers.get_all(name, []) != [value]
                   for name, value in expected['headers'].items()):
                raise OperationError('Publisher response metadata differs from the candidate')
            if response.read(MAX_BODY_BYTES + 1) != body:
                raise OperationError('Publisher response body differs from the candidate')
        except (OSError, http.client.HTTPException):
            # HTTP exception details may contain a credential-bearing route.
            raise OperationError('Publisher acknowledgement is unavailable') from None
        finally:
            connection.close()


def _wait_publication(directory: Path) -> None:
    """Bound retries: at most 15 two-route probes with one-second I/O waits."""
    for attempt in range(15):
        try:
            _publication_health(directory)
            return
        except (OSError, OperationError):
            if attempt == 14:
                raise
            time.sleep(0.2)


def apply(store: StateStore, identity: str) -> dict:
    with store.exclusive():
        kind, payload = store.get(identity)
        if kind != 'deployment':
            raise OperationError('Plan type is not deployment')
        activation = _activation(payload)
        directory = _managed_revision(ROOT/'revisions'/payload['revision'])
        _verify_candidate(directory, payload)
        config = json.loads((directory/'config.json').read_text(encoding='utf-8'))
        server = config['server']
        if 'certificate_bindings' in payload:
            if _certificate_bindings(config) != payload['certificate_bindings']:
                raise OperationError('A certificate binding changed after validation')
        else:
            certificate = validate_certificate(server['certificate_path'], server['key_path'],
                                               [server['site_domain'], server['subscription_domain']])
            if (certificate['sha256'] != payload['certificate_sha256']
                    or _certificate_files(server) != payload['certificate_files']):
                raise OperationError('Deployment certificate changed after validation')
        previous = _current_revision()
        if activation == 'publication':
            _publication_server(server, previous, config)
        elif activation == 'reality':
            _reality_transition(config,previous,payload.get('reality_operation'))
        elif activation == 'web':
            _web_server(config,previous)
        else:
            _preserve_endpoints(config,previous)
        if 'web' in config:
            _check_new_ports(config,previous)
            if activation != 'publication':
                _check_new_domains(config,previous)
        _check_runtime_permissions(directory)
        if 'reality' in config:
            _check_reality_ports(config,previous)
        if activation=='reality':
            reality.probe_targets(_reality_scope(config,'primary'))
        if previous is None:
            _check_ports(server)
        store.record(identity,'activating',{'previous':str(previous) if previous else None,
                     'candidate':str(directory),'activation':activation})
        try:
            _link(directory)
            _publish(directory)
            if activation == 'publication':
                run(['systemctl', 'reload', 'proxyhub-publisher.service'])
                _wait_publication(directory)
            elif activation == 'web':
                run(['systemctl','reload','proxyhub-nginx.service'])
                run(['systemctl','reload','proxyhub-publisher.service'])
                _wait_web_health(config,directory)
                if 'reality' in config:
                    _wait_local_reality_health(config,directory)
                _wait_publication(directory)
            elif activation == 'reality':
                old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
                before, after = _reality_units(old), _reality_units(config)
                for entry_id,unit in after.items():
                    if entry_id not in before:
                        run(['systemctl','start',unit])
                run(['systemctl','reload','proxyhub-nginx.service'])
                _wait_web_health(config,directory)
                _wait_reality_health(_reality_scope(config,'primary'),directory)
                run(['systemctl','reload','proxyhub-publisher.service'])
                _wait_publication(directory)
                for entry_id,unit in before.items():
                    if entry_id not in after:
                        run(['systemctl','stop',unit])
                        run(['systemctl','disable',unit])
                for entry_id,unit in after.items():
                    if entry_id not in before:
                        run(['systemctl','enable',unit])
            else:
                for service in _core_units(config):
                    run(['systemctl','restart',service])
                if 'reality' not in config:
                    _wait_health(server)
                if 'web' in config:
                    _wait_web_health(config,directory)
                elif payload.get('website_change') or config.get('website_content'):
                    _website_health(server, directory)
                if 'reality' in config:
                    _wait_local_reality_health(config,directory)
                run(['systemctl','enable',*_core_units(config)])
            store.commit(identity)
        except Exception:
            # A successful database COMMIT is the boundary. Never undo it if a
            # later Python exception occurs; recovery sees no pending activation.
            if store.inspect(identity)['status'] != 'applied':
                if activation=='reality':
                    _restore(previous,directory,activation,payload.get('reality_operation'))
                else:
                    _restore(previous, directory, activation)
                store.abort(identity, {'previous_available': previous is not None})
            raise
        return {'status':'applied','revision':payload['revision'],'activation':activation,
                'runtime_health':'passed','real_client_acceptance':'required'}
