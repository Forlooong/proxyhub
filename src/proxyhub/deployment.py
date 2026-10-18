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
import subprocess
import tempfile
import time

from .core import OperationError, atomic_write, canonical, digest, run
from .proxy import validate_spec, render_sing_box
from .site import render_nginx, render_site, validate_certificate
from .publication import (build_bundle, save_bundle, validate_bundle, MAX_BODY_BYTES,
                          iter_routes, load_security, save_security, advance_security, route_allowed)
from .publication import token_digest, alias_digest
from .state import StateStore
from .websites import normalize_web, get_site, get_certificate, consumers, site_directory, sync_default
from . import reality, hy2, nodes, subscriptions

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
    shared = directory/'shared.json'
    if 'shared.json' in manifest:
        if shared.is_symlink() or not shared.is_file() or digest(shared.read_bytes()) != manifest['shared.json']:
            raise OperationError('Shared runtime changed after validation')
        allowed.add('shared.json')
    elif shared.exists() or shared.is_symlink():
        raise OperationError('Candidate contains an unmanaged shared runtime')
    if hy2.normalize_hy2(config) is not None:
        file = directory/'hy2.json'
        if file.is_symlink() or not file.is_file() or digest(file.read_bytes()) != manifest.get('hy2.json'):
            raise OperationError('HY2 candidate file changed after validation')
        allowed.add('hy2.json')
    elif (directory/'hy2.json').exists() or (directory/'hy2.json').is_symlink():
        raise OperationError('Candidate contains an unmanaged HY2 configuration')
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
    runtime = directory/'reality'
    if runtime.exists() or runtime.is_symlink():
        if runtime.is_symlink() or not runtime.is_dir() or {path.relative_to(directory).as_posix() for path in runtime.iterdir()} != expected:
            raise OperationError('Candidate contains an unmanaged REALITY configuration')
    expected_nodes = {'nodes/'+node['id']+'.json' for node in nodes.normalize_nodes(config)}
    for name in expected_nodes:
        file = directory/name
        if ((directory/'nodes').is_symlink() or file.is_symlink() or not file.is_file()
                or digest(file.read_bytes()) != manifest.get(name)):
            raise OperationError('Node candidate file changed after validation')
    if set(manifest) != allowed | expected | expected_nodes:
        raise OperationError('Candidate file manifest contains an unmanaged resource')
    runtime = directory/'nodes'
    if runtime.exists() or runtime.is_symlink():
        if (runtime.is_symlink() or not runtime.is_dir()
                or {path.relative_to(directory).as_posix() for path in runtime.iterdir()} != expected_nodes):
            raise OperationError('Candidate contains an unmanaged node configuration')
    parent = directory/'sites'
    if parent.exists() or parent.is_symlink():
        if parent.is_symlink() or not parent.is_dir():
            raise OperationError('Unsafe additional website directory')
        expected = {site['id'] for site in sites if site['id'] != 'default'}
        if {path.name for path in parent.iterdir()} != expected:
            raise OperationError('Candidate contains an unmanaged website tree')


def client_config(server: dict, overrides: dict, sni: str | None = None, hy2_node: dict | None = None,
                  extra_nodes: list | None = None, node_ids: list[str] | None = None) -> dict:
    node = {key: server[key] for key in ('uuid', 'public_key', 'short_id')}
    node.update(id='local-reality', name=server['node_name'], server=server['public_address'],
                port=server['public_port'], server_name=sni or server['site_domain'])
    source = {'node':node,'overrides':overrides}
    if hy2_node is not None:
        source['hy2'] = hy2_node
    if extra_nodes:
        source['nodes'] = extra_nodes
    if node_ids is not None:
        source['node_ids'] = node_ids
    text = run([str(BIN/'node'), str(APP/'packages/converter/dist/worker.js')], input=json.dumps(source), timeout=20)
    return json.loads(text)


def _generated_client(config: dict, node_ids: list[str] | None = None) -> dict:
    sni = None
    if 'reality' in config:
        sni = next(entry['sni'] for entry in reality.public_resources(config)['endpoints'] if entry['primary'])
    extra = [snapshot for node in nodes.normalize_nodes(config)
             if (snapshot := nodes.client_node(config,node)) is not None]
    resource = hy2.client_node(config) if _hy2_enabled(config) else None
    if node_ids is not None:
        return client_config(config['server'],config['overrides'],sni,resource,extra,node_ids)
    if extra:
        return client_config(config['server'],config['overrides'],sni,resource,extra)
    if resource is not None:
        return client_config(config['server'],config['overrides'],sni,resource)
    if sni is not None:
        return client_config(config['server'],config['overrides'],sni)
    return client_config(config['server'],config['overrides'])


def _generated_profiles(config: dict, default: dict) -> list[dict]:
    profiles = []
    for profile in subscriptions.normalize(config):
        if not profile['enabled']:
            continue
        if profile['id']=='default' and profile['node_ids'] is None:
            generated = default
        else:
            selected = subscriptions.selected_node_ids(config,profile)
            generated = _generated_client({**config,'overrides':profile['overrides']},selected)
        entry = {key:profile[key] for key in ('id','host','token','name','userinfo','update_interval_hours',
                                             'reset_day','reset_at','path_prefix','aliases','formats')}
        entry.update(yaml=generated['yaml'],raw=generated['raw'])
        profiles.append(entry)
    return profiles


def _subscription_transition(config: dict, previous: Path | None, operation: dict | None) -> None:
    if operation is not None and (not isinstance(operation,dict)
            or set(operation)!={'kind','subscription_id'} or operation['kind'] not in subscriptions.KINDS
            or not isinstance(operation['subscription_id'],str)):
        raise OperationError('Invalid subscription lifecycle operation')
    if previous is None:
        return
    before = {item['id']:item for item in subscriptions.normalize(
        json.loads((previous/'config.json').read_text(encoding='utf-8')))}
    after = {item['id']:item for item in subscriptions.normalize(config)}
    if operation is not None:
        try:
            security = load_security(ROOT/'publish/security.json')
        except (OSError,ValueError):
            raise OperationError('Publication safety state is unavailable') from None
        for identity,last in after.items():
            first = before.get(identity)
            old_routes = {(entry['host'],entry['path_prefix']) for entry in
                          ([first,*first['aliases']] if first else [])}
            if ((first is None or first['token']!=last['token'])
                    and token_digest(last['token']) in security['revoked_tokens']):
                raise OperationError('A subscription cannot introduce a revoked token')
            for entry in [last,*last['aliases']]:
                if ((entry['host'],entry['path_prefix']) not in old_routes
                        and not route_allowed(security,entry['host'],entry['path_prefix'],last['token'])):
                    raise OperationError('A subscription cannot introduce a retired route')
    for identity in before.keys() & after.keys():
        first, last = before[identity], after[identity]
        if first['token']==last['token']:
            continue
        if operation=={'kind':'subscription.token.rotate','subscription_id':identity}:
            if {k:v for k,v in first.items() if k!='token'} != {k:v for k,v in last.items() if k!='token'}:
                raise OperationError('Token rotation cannot change subscription settings')
            continue
        # Restoring a revoked historical token is safe only as a closed route;
        # restoration itself must not revoke the newer valid token.
        try:
            revoked = load_security(ROOT/'publish/security.json')['revoked_tokens']
        except (OSError,ValueError):
            revoked = []
        if operation is not None or token_digest(last['token']) not in revoked:
            raise OperationError('Subscription token changes require an explicit token rotation')
    if operation and operation['kind'] in ('subscription.token.rotate','subscription.alias.retire'):
        identity = operation['subscription_id']
        if identity not in before or identity not in after:
            raise OperationError('Subscription safety operation requires an existing identity')
        first, last = before[identity], after[identity]
        if operation['kind']=='subscription.token.rotate' and first['token']==last['token']:
            raise OperationError('Token rotation must replace the selected subscription token')
        if operation['kind']=='subscription.alias.retire':
            if ({k:v for k,v in first.items() if k!='aliases'} != {k:v for k,v in last.items() if k!='aliases'}
                    or len(first['aliases'])!=len(last['aliases'])+1
                    or any(alias not in first['aliases'] for alias in last['aliases'])):
                raise OperationError('Alias retirement must remove exactly one existing alias')
            retired = next(alias for alias in first['aliases'] if alias not in last['aliases'])
            if any((entry['host'],entry['path_prefix'])==(retired['host'],retired['path_prefix'])
                   for other in before.values() if other['id']!=identity
                   for entry in [other,*other['aliases']]):
                raise OperationError('Alias route is still referenced by another subscription')


def _certificate_bindings(config: dict) -> dict:
    web = normalize_web(config)
    result = {}
    for binding in web['certificates']:
        active = [item['domain'] for item in consumers(web,config['server'],binding['id'],config.get('reality'),config.get('hy2'),config.get('nodes'))
                  if item.get('enabled',True) and item['domain']]
        details = validate_certificate(binding['certificate_path'],binding['key_path'],
                                       list(dict.fromkeys(active))) if active else {'sha256':None}
        result[binding['id']] = {'sha256':details['sha256'],'files':_certificate_files(binding)}
    return result


def _preserve_endpoints(config: dict, previous: Path | None, *, reality_operation: dict | None = None,
                        hy2_operation: dict | None = None, node_operation: dict | None = None) -> None:
    if previous is None:
        if nodes.normalize_nodes(config):
            raise OperationError('Create nodes through typed plans after the initial deployment')
        if hy2.normalize_hy2(config) is not None:
            raise OperationError('Create HY2 through a typed plan after the initial deployment')
        return
    old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
    if node_operation is None and nodes.normalize_nodes(config) != nodes.normalize_nodes(old):
        raise OperationError('Node resources require an explicit lifecycle plan')
    if hy2_operation is None and hy2.normalize_hy2(config) != hy2.normalize_hy2(old):
        raise OperationError('HY2 resources require an explicit creation, update, enable or disable plan')
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
                    and consumers(old_web,old['server'],binding['id'],old.get('reality'),old.get('hy2'),old.get('nodes'))):
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


def _hy2_transition(config: dict, previous: Path | None, operation: dict) -> None:
    if previous is None or not isinstance(operation,dict) or set(operation)!={'kind'}:
        raise OperationError('HY2 activation requires an existing deployment and a typed transition')
    old = sync_default(json.loads((previous/'config.json').read_text(encoding='utf-8')))
    old['server'] = validate_spec(old['server'])
    if ({key:value for key,value in config.items() if key!='hy2'}
            != {key:value for key,value in old.items() if key!='hy2'}):
        raise OperationError('HY2 activation cannot change REALITY, websites or subscription settings')
    before, after = hy2.normalize_hy2(old), hy2.normalize_hy2(config)
    kind = operation['kind']
    if kind == 'create':
        valid = before is None and after is not None and after['enabled']
    elif kind == 'update':
        allowed = {'name','port','server_name','certificate_id'}
        valid = (before is not None and after is not None and before!=after
                 and {k:v for k,v in before.items() if k not in allowed}
                 == {k:v for k,v in after.items() if k not in allowed})
    elif kind in ('enable','disable'):
        valid = (before is not None and after is not None and before['enabled']!=after['enabled']
                 and after['enabled']==(kind=='enable')
                 and {k:v for k,v in before.items() if k!='enabled'}
                 == {k:v for k,v in after.items() if k!='enabled'})
    else:
        valid = False
    if not valid:
        raise OperationError('HY2 transition differs from the approved operation')
    _preserve_endpoints(config,previous,hy2_operation=operation)


def _certificate_material(path: str) -> list[str]:
    return Path(path).read_text(encoding='ascii').splitlines()


def _nodes_transition(config: dict, previous: Path | None, operation: dict) -> None:
    if previous is None or not isinstance(operation,dict) or set(operation)!={'kind','node_id'}:
        raise OperationError('Node activation requires an existing deployment and a typed transition')
    old = sync_default(json.loads((previous/'config.json').read_text(encoding='utf-8')))
    old['server'] = validate_spec(old['server'])
    if ({key:value for key,value in config.items() if key!='nodes'}
            != {key:value for key,value in old.items() if key!='nodes'}):
        raise OperationError('Node activation cannot change existing services or subscription settings')
    before = {item['id']:item for item in nodes.normalize_nodes(old)}
    after = {item['id']:item for item in nodes.normalize_nodes(config)}
    identity, kind = operation['node_id'], operation['kind']
    if kind == 'create':
        valid = (identity not in before and identity in after and after[identity]['enabled']
                 and set(after)==set(before)|{identity})
    elif kind == 'delete':
        valid = (identity in before and not before[identity]['enabled']
                 and set(after)==set(before)-{identity})
    elif kind in ('update','enable','disable'):
        valid = identity in before and set(before)==set(after)
        if valid:
            first, last = before[identity], after[identity]
            editable = {'name','port','options'} if kind=='update' else {'enabled'}
            valid = (first!=last and {k:v for k,v in first.items() if k not in editable}
                     == {k:v for k,v in last.items() if k not in editable})
            if kind!='update':
                valid = valid and last['enabled']==(kind=='enable')
    else:
        valid = False
    if not valid or any(after.get(key)!=value for key,value in before.items() if key!=identity):
        raise OperationError('Node transition differs from the approved resource operation')
    _preserve_endpoints(config,previous,node_operation=operation)


def _render_node(config: dict, node: dict) -> dict:
    rendered = nodes.render_node(config,node)
    tls = rendered['inbounds'][0].get('tls',{})
    if 'certificate_path' in tls:
        tls['certificate'] = _certificate_material(tls.pop('certificate_path'))
        tls['key'] = _certificate_material(tls.pop('key_path'))
    return rendered


def _render_hy2(config: dict) -> dict:
    rendered = hy2.render_hy2(config)
    tls = rendered['inbounds'][0]['tls']
    # The archive stays root-only. The already private proxy-group runtime
    # receives only its selected material, bound by the candidate manifest.
    tls['certificate'] = _certificate_material(tls.pop('certificate_path'))
    tls['key'] = _certificate_material(tls.pop('key_path'))
    return rendered


def _render_shared(config: dict) -> dict:
    rendered = nodes.render_shared(config) or {'log':{'level':'warn','timestamp':True},
        'inbounds':[], 'outbounds':[{'type':'direct','tag':'direct'}]}
    for inbound in rendered['inbounds']:
        tls = inbound.get('tls',{})
        if 'certificate_path' in tls:
            tls['certificate'] = _certificate_material(tls.pop('certificate_path'))
            tls['key'] = _certificate_material(tls.pop('key_path'))
    return rendered


def _runtime_transition(config: dict, previous: Path | None) -> None:
    if previous is None or config != json.loads((previous/'config.json').read_text(encoding='utf-8')):
        raise OperationError('Runtime migration must preserve every resource and credential')
    if (previous/'shared.json').exists():
        raise OperationError('The fixed shared runtime is already in use')


def prepare(store: StateStore, config: dict, *, expected_revision: int | None = None,
            publish_only: bool = False, site_files: dict[str, bytes] | None = None,
            website_change: bool = False, site_id: str = 'default', web_only: bool = False,
            reality_operation: dict | None = None, hy2_operation: dict | None = None,
            node_operation: dict | None = None, runtime_only: bool = False,
            subscription_operation: dict | None = None) -> dict:
    base_revision = store.revision if expected_revision is None else expected_revision
    if store.revision != base_revision:
        raise OperationError('Configuration changed before preparation')
    if store.pending():
        raise OperationError('Recover the interrupted activation before preparing a deployment')
    if (not {'server','subscription','overrides'} <= set(config)
            or set(config) - {'server','subscription','subscriptions','overrides','website_content','web','reality','hy2','nodes'}):
        raise OperationError('Deployment contains unknown or missing objects')
    if (type(publish_only) is not bool or type(web_only) is not bool or type(runtime_only) is not bool
            or sum((publish_only,web_only,runtime_only,reality_operation is not None,hy2_operation is not None,node_operation is not None))>1):
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
    _subscription_transition(config,previous,subscription_operation)
    if publish_only:
        _publication_server(server,previous,config)
        if site_files is not None:
            raise OperationError('Publication-only activation cannot change website content')
    if runtime_only:
        _runtime_transition(config,previous)
        if site_files is not None:
            raise OperationError('Runtime migration cannot change website content')
    elif reality_operation is not None:
        _reality_transition(config,previous,reality_operation)
    elif hy2_operation is not None:
        _hy2_transition(config,previous,hy2_operation)
    elif node_operation is not None:
        _nodes_transition(config,previous,node_operation)
    elif web_only:
        _web_server(config,previous)
    elif not publish_only:
        _preserve_endpoints(config,previous)
    if not publish_only:
        _check_new_domains(config,previous)
    if reality_operation is not None:
        reality.probe_targets(_reality_scope(config,'primary'))
    if node_operation is not None:
        _probe_node_targets(config,{node_operation['node_id']})
    bindings = _certificate_bindings(config)
    resource = hy2.normalize_hy2(config)
    generated = _generated_client(config)
    profiles = _generated_profiles(config,generated)
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
        if resource is not None:
            path = directory/'hy2.json'
            atomic_write(path,canonical(_render_hy2(config)),0o640)
            os.chown(path,0,proxy_gid)
            run([str(BIN/'sing-box'),'check','-c',str(path)])
        for node in nodes.normalize_nodes(config):
            path = directory/'nodes'/(node['id']+'.json')
            atomic_write(path,canonical(_render_node(config,node)),0o640)
            path.parent.chmod(0o711)
            os.chown(path,0,proxy_gid)
            run([str(BIN/'sing-box'),'check','-c',str(path)])
        if runtime_only or previous is None or (previous/'shared.json').is_file():
            path = directory/'shared.json'
            atomic_write(path,canonical(_render_shared(config)),0o640)
            os.chown(path,0,proxy_gid)
            run([str(BIN/'sing-box'),'check','-c',str(path)])
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
        save_bundle(directory/'bundle.json',build_bundle(profiles,revision))
        atomic_write(directory/'config.json',canonical(config))
        atomic_write(directory/'client.yaml',generated['yaml'].encode())
        run([str(BIN/'sing-box'),'check','-c',str(singbox)])
        run([str(BIN/'nginx'),'-e','stderr','-p',str(ROOT/'nginx')+'/','-t','-c',str(directory/'nginx.conf')])
        run([str(BIN/'mihomo'),'-t','-d',str(ROOT/'client-check'),'-f',str(directory/'client.yaml')])
        _check_new_ports(config,previous)
        _check_hy2_ports(config,previous)
        _check_node_ports(config,previous)
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
                   'activation':('runtime' if runtime_only else 'node' if node_operation is not None else 'hy2' if hy2_operation is not None else 'reality' if reality_operation is not None else
                                 'publication' if publish_only else 'web' if web_only else 'full')}
        if reality_operation is not None:
            payload['reality_operation'] = reality_operation
        if hy2_operation is not None:
            payload['hy2_operation'] = hy2_operation
        if node_operation is not None:
            payload['node_operation'] = node_operation
        if subscription_operation is not None:
            payload['subscription_operation'] = subscription_operation
        if config.get('website_content'):
            payload['content_version'] = config['website_content']
        if 'default' in summaries:
            payload['website_summary'] = summaries['default']
        if website_change:
            payload.update(website_change=True,website_id=site_id)
        plan_id = store.plan('deployment',payload,expected_revision=base_revision)
        changes = (['subscription bundle','publisher reload'] if publish_only else
                   ['fixed shared HY2/additional-node runtime','all enabled group members reconnect','REALITY and websites remain running'] if runtime_only else
                   ['selected protocol node and service','subscription publication'] if node_operation is not None else
                   ['HY2 resource and service','subscription publication'] if hy2_operation is not None else
                   ['new or retained REALITY entries','Nginx reload','subscription SNI publication'] if reality_operation is not None else
                   ['website resources','Nginx reload','publisher reload'] if web_only else
                   ['managed websites','REALITY listener','subscription bundle','ProxyHub service activation'])
        shared_runtime = _runtime_impact(config,directory,previous)
        if shared_runtime is not None and (node_operation is not None or hy2_operation is not None):
            changes = ['selected protocol resource','subscription publication',
                       'shared HY2/additional-node runtime restart; all enabled members reconnect'
                       if shared_runtime['changed'] else 'shared runtime unchanged']
        return {'plan_id':plan_id,'revision':revision,'site_domain':server['site_domain'],
                'subscription_domain':server['subscription_domain'],'checks':['certificates','sing-box','nginx','mihomo'],
                'activation':payload['activation'],'changes':changes,
                'shared_runtime':shared_runtime}
    except Exception:
        shutil.rmtree(directory)
        raise



def _activation(payload: dict) -> str:
    """Missing mode identifies a phase-1 full-deployment record."""
    mode = payload.get('activation', 'full')
    if mode not in ('full', 'publication', 'web', 'reality', 'hy2', 'node', 'runtime'):
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
    if config is not None and nodes.normalize_nodes(config) != nodes.normalize_nodes(current):
        raise OperationError('Publication-only activation cannot change node resources')
    if config is not None and hy2.normalize_hy2(config) != hy2.normalize_hy2(current):
        raise OperationError('Publication-only activation cannot change HY2 resources')
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


def _ensure_publication_security(store: StateStore, data_root: Path | None = None) -> dict:
    """Initialize once; a missing ledger after initialization must never reset it."""
    import pwd
    root = ROOT if data_root is None else data_root
    path = root/'publish/security.json'
    if path.parent.is_symlink():
        raise OperationError('Unsafe publication security directory')
    row = store.db.execute("SELECT value FROM metadata WHERE key='publication_security_generation'").fetchone()
    if not path.exists() and not path.is_symlink() and row is None:
        path.parent.mkdir(parents=True,exist_ok=True)
        save_security(path,{'schema_version':1,'generation':0,'revoked_tokens':[],'retired_aliases':[]})
    try:
        security = load_security(path)
    except (OSError,ValueError):
        raise OperationError('Publication security state is unavailable; publishing must remain closed') from None
    if row is not None and security['generation']<int(row[0]):
        raise OperationError('Publication security generation cannot go backwards')
    os.chown(path,0,pwd.getpwnam('proxyhub-pub').pw_gid)
    path.chmod(0o640)
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('publication_security_generation',?)",
                         (str(security['generation']),))
    return security


def _publication_revocations(previous: Path | None, directory: Path, operation: dict | None):
    """Derive the same deny set for activation, recovery and the startup guard."""
    if previous is None or operation is None or operation['kind'] not in (
            'subscription.token.rotate','subscription.alias.retire'):
        return [], []
    before = subscriptions.get_profile(json.loads((previous/'config.json').read_text(encoding='utf-8')),
                                       operation['subscription_id'])
    after = subscriptions.get_profile(json.loads((directory/'config.json').read_text(encoding='utf-8')),
                                      operation['subscription_id'])
    revoked, retired = [], []
    if operation['kind']=='subscription.token.rotate':
        revoked.append(before['token'])
    else:
        routes = {(entry['host'],entry['path_prefix']) for entry in after['aliases']}
        retired.extend({'host':entry['host'],'path_prefix':entry['path_prefix']}
                       for entry in before['aliases']
                       if (entry['host'],entry['path_prefix']) not in routes)
    return revoked, retired


def _publication_gate(*, closed: bool) -> None:
    """A reversible read gate for the existing unprivileged publisher.

    The existing activation journal must own the operation before this closes.
    A previously passed startup check and existing listeners cannot bypass the
    closed gate. An interrupted owner is resumed only through normal recovery.
    """
    path = ROOT/'publish'
    if path.is_symlink() or not path.is_dir():
        raise OperationError('Unsafe publication security directory')
    path.chmod(0o700 if closed else 0o750)
    if os.name == 'posix':
        descriptor = os.open(path,os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _revoke_publication_changes(store: StateStore, previous: Path | None, directory: Path,
                                operation: dict | None = None) -> None:
    """Safety changes survive candidate failure and ordinary configuration recovery."""
    _ensure_publication_security(store)
    revoked, retired = _publication_revocations(previous,directory,operation)
    if revoked or retired:
        advance_security(ROOT/'publish/security.json',revoked,retired)
        _ensure_publication_security(store)
        _publication_gate(closed=False)


def _publication_nginx_changed(directory: Path, previous: Path | None) -> bool:
    if previous is None:
        return True
    def normalized(path):
        return (path/'nginx.conf').read_text(encoding='utf-8').replace(str(path),'<revision>')
    return normalized(directory)!=normalized(previous)


def _units() -> tuple[str, ...]:
    from .installer import UNITS
    return UNITS


def _reality_units(config: dict) -> dict[str,str]:
    return {entry['id']:('proxyhub-proxy.service' if entry['id']=='default' else
                         'proxyhub-reality@'+entry['id']+'.service')
            for entry in reality.normalize_reality(config)['endpoints']}


def _runtime_units(config: dict, directory: Path | None) -> tuple[str,...]:
    if directory is not None and (directory/'shared.json').is_file():
        return ('proxyhub-shared.service',) if _runtime_members(config) else ()
    return (*_node_units(config).values(),*(('proxyhub-hy2.service',) if _hy2_enabled(config) else ()))


def _core_units(config: dict, directory: Path | None = None) -> tuple[str,...]:
    additional = _runtime_units(config,directory)
    if 'reality' not in config:
        return (*_units(),*additional)
    entries = _reality_units(config)
    return (*entries.values(),*(unit for unit in _units() if unit!='proxyhub-proxy.service'),*additional)


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
    if (directory/'shared.json').exists():
        extra.append(directory/'shared.json')
    if hy2.normalize_hy2(config) is not None:
        extra.append(directory/'hy2.json')
    node_files = [directory/'nodes'/(node['id']+'.json') for node in nodes.normalize_nodes(config)]
    if node_files:
        parents.append(directory/'nodes')
        extra.extend(node_files)
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


def _runtime_members(config: dict) -> list[dict]:
    resource = hy2.normalize_hy2(config)
    return ([{'id':resource['id'],'name':resource['name'],'protocol':'hy2'}]
            if resource is not None and resource['enabled'] else []) + [
        {key:node[key] for key in ('id','name','protocol')}
        for node in nodes.normalize_nodes(config) if node['enabled']]


def _runtime_impact(config: dict, directory: Path, previous: Path | None) -> dict | None:
    if not (directory/'shared.json').is_file():
        return None
    old = json.loads((previous/'config.json').read_text(encoding='utf-8')) if previous else {}
    changed = (previous is None or not (previous/'shared.json').is_file()
               or (directory/'shared.json').read_bytes() != (previous/'shared.json').read_bytes())
    members = {member['id']:member for member in [*_runtime_members(old),*_runtime_members(config)]}
    return {'changed':changed,'members':list(members.values()),'reconnect_required':changed and bool(members)}


def _sync_runtime(config: dict, old: dict, directory: Path, previous: Path | None) -> tuple[bool,set[str]]:
    shared = (directory/'shared.json').is_file()
    was_shared = previous is not None and (previous/'shared.json').is_file()
    if not shared and not was_shared:
        return (_sync_hy2(config,old,directory=directory,previous=previous),
                _sync_nodes(config,old,directory,previous))
    before, after = _runtime_units(old,previous), _runtime_units(config,directory)
    changed = shared != was_shared or previous is None or (
        shared and (directory/'shared.json').read_bytes() != (previous/'shared.json').read_bytes())
    if not changed:
        return False,set()
    # Stop the old topology before binding any of its retained ports. The old
    # units/configuration stay available until the journal commits, and on disk
    # afterwards for recovery; no resource or credential is deleted.
    for unit in before:
        if not (shared and was_shared and unit in after):
            run(['systemctl','stop',unit])
            run(['systemctl','disable',unit])
    for unit in after:
        run(['systemctl','restart' if shared and was_shared and unit in before else 'start',unit])
        run(['systemctl','enable',unit])
    return _hy2_enabled(config),{node['id'] for node in nodes.normalize_nodes(config) if node['enabled']}


def _runtime_health(config: dict, directory: Path, changed: tuple[bool,set[str]]) -> None:
    changed_hy2, changed_nodes = changed
    if changed_hy2:
        _wait_hy2_health(config,directory)
    _wait_nodes_health(config,directory,changed_nodes)


def _isolated_runtime_health(config: dict, directory: Path) -> None:
    """Authenticate the migration candidate before stopping the old listeners."""
    if not _runtime_members(config):
        return
    import pwd
    account = pwd.getpwnam('proxyhub-proxy')
    probe = json.loads(json.dumps(config))
    rendered = json.loads((directory/'shared.json').read_text(encoding='utf-8'))
    ports = set()
    for inbound in rendered['inbounds']:
        while True:
            with socket.socket() as tcp, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
                tcp.bind(('127.0.0.1',0))
                port = tcp.getsockname()[1]
                try:
                    udp.bind(('127.0.0.1',port))
                except OSError:
                    continue
            if port not in ports:
                ports.add(port)
                break
        inbound.update(listen='127.0.0.1',listen_port=port)
        if inbound['tag']=='hy2-default':
            probe['hy2']['port'] = port
        else:
            next(node for node in probe['nodes'] if node['listener_id']==inbound['tag'])['port'] = port
    with tempfile.TemporaryDirectory(prefix='.shared-health-',dir=ROOT) as temp:
        parent = Path(temp)
        os.chown(parent,0,account.pw_gid)
        parent.chmod(0o710)
        path = parent/'shared.json'
        atomic_write(path,canonical(rendered),0o640)
        os.chown(path,0,account.pw_gid)
        process = subprocess.Popen([str(BIN/'sing-box'),'run','-c',str(path)],
            user=account.pw_uid,group=account.pw_gid,extra_groups=[],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            _wait_hy2_health(probe,directory)
            _wait_nodes_health(probe,directory)
            if process.poll() is not None:
                raise OperationError('Isolated shared runtime exited before validation')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _hy2_enabled(config: dict) -> bool:
    resource = hy2.normalize_hy2(config)
    return resource is not None and resource['enabled']


def _check_hy2_ports(config: dict, previous: Path | None) -> None:
    if not _hy2_enabled(config):
        return
    resource = hy2.normalize_hy2(config)
    old = json.loads((previous/'config.json').read_text(encoding='utf-8')) if previous else {}
    if _hy2_enabled(old) and hy2.normalize_hy2(old)['port']==resource['port']:
        return
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as checked:
            checked.bind(('0.0.0.0',resource['port']))
    except OSError:
        raise OperationError('HY2 UDP port is unavailable; no service was replaced') from None


def _hy2_changed(config: dict, old: dict) -> bool:
    return (_hy2_enabled(config)!=_hy2_enabled(old)
            or (_hy2_enabled(config) and hy2.render_hy2(config)!=hy2.render_hy2(old)))


def _sync_hy2(config: dict, old: dict, *, restore: bool = False,
              directory: Path | None = None, previous: Path | None = None) -> bool:
    changed = (restore or _hy2_changed(config,old)
               or (_hy2_enabled(config) and _hy2_enabled(old) and directory is not None and previous is not None
                   and (directory/'hy2.json').read_bytes() != (previous/'hy2.json').read_bytes()))
    if not changed:
        return False
    unit = 'proxyhub-hy2.service'
    if _hy2_enabled(config):
        run(['systemctl','restart' if restore or _hy2_enabled(old) else 'start',unit])
        run(['systemctl','enable',unit])
    else:
        run(['systemctl','stop',unit])
        run(['systemctl','disable',unit])
    return True


def _hy2_probe(config: dict, fingerprint: str | None = None) -> None:
    resource = hy2.normalize_hy2(config)
    _proxy_probe(config,{'type':'hysteria2','port':resource['port'],'password':resource['password'],
                        'sni':resource['server_name'],'skip-cert-verify':False},fingerprint)


def _proxy_probe(config: dict, outbound: dict, fingerprint: str | None = None) -> None:
    with socket.socket() as available:
        available.bind(('127.0.0.1',0))
        port = available.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='.hy2-health-',dir=ROOT) as temp:
        path = Path(temp)/'client.json'
        client = {'mixed-port':port,'allow-lan':False,'bind-address':'127.0.0.1',
                  'mode':'rule','log-level':'silent',
                  'proxies':[{**outbound,'name':'HY2-HEALTH','server':'127.0.0.1'}],
                  'rules':['MATCH,HY2-HEALTH']}
        if fingerprint is not None:
            client['proxies'][0]['fingerprint'] = fingerprint
        atomic_write(path,canonical(client))
        process = subprocess.Popen([str(BIN/'mihomo'),'-d',temp,'-f',str(path)],
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic()+3
            connected = False
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise OperationError('HY2 verification client could not start')
                try:
                    stream = socket.create_connection(('127.0.0.1',port),timeout=min(.2,max(.001,deadline-time.monotonic())))
                    connected = True
                    with stream:
                        stream.settimeout(max(.001,deadline-time.monotonic()))
                        target = config['server']['publisher_port']
                        stream.sendall(f'GET http://127.0.0.1:{target}/__hy2_health__ HTTP/1.1\r\nHost: 127.0.0.1:{target}\r\nConnection: close\r\n\r\n'.encode())
                        response = http.client.HTTPResponse(stream)
                        try:
                            response.begin()
                            if response.status==404:
                                return
                            if response.status!=502:
                                raise OperationError('HY2 authenticated QUIC or selected certificate check failed')
                        finally:
                            response.close()
                except (OSError,http.client.HTTPException):
                    pass
                # Mihomo listens before its tunnel enters Running. A listening
                # port (and its temporary 502) is not authenticated readiness.
                remaining = deadline-time.monotonic()
                if remaining>0:
                    time.sleep(min(.1,remaining))
            if not connected:
                raise OperationError('HY2 verification client is unavailable')
            raise OperationError('HY2 authenticated QUIC or selected certificate check failed')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _runtime_fingerprint(path: Path) -> str:
    """Pin the immutable deployed leaf, including during restoration of old PEM."""
    try:
        material = json.loads(path.read_text(encoding='utf-8'))['inbounds'][0]['tls']['certificate']
        if not isinstance(material,list) or not material or any(not isinstance(line,str) for line in material):
            raise ValueError
        pem = '\n'.join(material)
        marker = '-----END CERTIFICATE-----'
        end = pem.index(marker) + len(marker)
        leaf = ssl.PEM_cert_to_DER_cert(pem[:end])
        if not leaf:
            raise ValueError
        return digest(leaf)
    except (OSError,ValueError,KeyError,IndexError,TypeError):
        raise OperationError('Immutable runtime certificate is invalid') from None


def _hy2_health(config: dict, directory: Path) -> None:
    if not _hy2_enabled(config):
        return
    # Mihomo's leaf pin replaces PKI verification. Require a separate ordinary
    # trust/name probe as well as the exact selected leaf, never treat a pin as PKI.
    fingerprint = _runtime_fingerprint(directory/'hy2.json')
    _hy2_probe(config)
    _hy2_probe(config,fingerprint)


def _wait_hy2_health(config: dict, directory: Path) -> None:
    for attempt in range(3):
        try:
            _hy2_health(config,directory)
            return
        except OperationError:
            if attempt==2:
                raise
            time.sleep(.2)


def _node_units(config: dict) -> dict[str,str]:
    return {node['id']:'proxyhub-node@'+node['id']+'.service'
            for node in nodes.normalize_nodes(config) if node['enabled']}


def _node_changes(config: dict, old: dict, directory: Path, previous: Path) -> set[str]:
    before = {node['id']:node for node in nodes.normalize_nodes(old)}
    after = {node['id']:node for node in nodes.normalize_nodes(config)}
    changed = set()
    for identity in before.keys() | after.keys():
        first, last = before.get(identity), after.get(identity)
        if not ((first and first['enabled']) or (last and last['enabled'])):
            continue
        if first!=last or (directory/'nodes'/(identity+'.json')).read_bytes() != (previous/'nodes'/(identity+'.json')).read_bytes():
            changed.add(identity)
    return changed


def _sync_nodes(config: dict, old: dict, directory: Path, previous: Path) -> set[str]:
    changed = _node_changes(config,old,directory,previous)
    before, after = _node_units(old), _node_units(config)
    for identity in sorted(changed):
        unit = 'proxyhub-node@'+identity+'.service'
        if identity in after:
            run(['systemctl','restart' if identity in before else 'start',unit])
            run(['systemctl','enable',unit])
        else:
            run(['systemctl','stop',unit])
            run(['systemctl','disable',unit])
    return changed


def _check_node_ports(config: dict, previous: Path | None) -> None:
    old = json.loads((previous/'config.json').read_text(encoding='utf-8')) if previous else config | {'nodes':[]}
    owned = {(node['id'],network,node['port']) for node in nodes.normalize_nodes(old) if node['enabled']
             for network in nodes.listener_networks(node)}
    for node in nodes.normalize_nodes(config):
        if not node['enabled']:
            continue
        for network in nodes.listener_networks(node):
            if (node['id'],network,node['port']) in owned:
                continue
            try:
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM if network=='udp' else socket.SOCK_STREAM) as checked:
                    if network=='tcp' and os.name=='posix':
                        # Match the listener's reuse policy: recently closed
                        # authenticated sessions must not block re-enabling it.
                        checked.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                    checked.bind(('0.0.0.0',node['port']))
            except OSError:
                raise OperationError('Node '+network.upper()+' port is unavailable; no service was replaced') from None


def _probe_node_targets(config: dict, identities: set[str]) -> None:
    selected = {node['options']['reality_target_id'] for node in nodes.normalize_nodes(config)
                if node['enabled'] and node['id'] in identities and node['options'].get('tls_mode')=='reality'}
    if not selected:
        return
    resources = reality.normalize_reality(config)
    entries = [entry for entry in resources['endpoints'] if entry['target_id'] in selected]
    scoped = {**config,'reality':{'targets':[target for target in resources['targets'] if target['id'] in selected],
              'endpoints':entries,'primary_endpoint_id':entries[0]['id']}}
    reality.probe_targets(scoped)


def _wait_nodes_health(config: dict, directory: Path, identities: set[str] | None = None) -> None:
    selected = [node for node in nodes.normalize_nodes(config) if node['enabled']
                and (identities is None or node['id'] in identities)]
    if not selected:
        return
    generated = _generated_client(config)
    outbounds = {outbound['name']:outbound for outbound in generated['proxies']}
    for node in selected:
        for attempt in range(3):
            try:
                _proxy_probe(config,outbounds[node['name']])
                if node['options'].get('tls_mode')=='certificate':
                    _proxy_probe(config,outbounds[node['name']],_runtime_fingerprint(directory/'nodes'/(node['id']+'.json')))
                break
            except OperationError:
                if attempt==2:
                    raise OperationError('Protocol node authentication or selected certificate check failed') from None
                time.sleep(.2)


def _restore(previous: Path | None, candidate: Path, activation: str = 'full',
             reality_operation: dict | None = None, hy2_operation: dict | None = None,
             node_operation: dict | None = None) -> None:
    current = _current_revision()
    if current not in (None, previous, candidate):
        raise OperationError('Refusing to overwrite an unexpected current revision')
    if activation in ('node','hy2','runtime'):
        config = json.loads((candidate/'config.json').read_text(encoding='utf-8'))
        if activation=='node':
            _nodes_transition(config,previous,node_operation)
        elif activation=='hy2':
            _hy2_transition(config,previous,hy2_operation)
        else:
            _runtime_transition(config,previous)
        restored = json.loads((previous/'config.json').read_text(encoding='utf-8'))
        if (candidate/'shared.json').is_file() and not (previous/'shared.json').is_file():
            run(['systemctl','stop','proxyhub-shared.service'])
        _link(previous)
        _publish(previous)
        changed = _sync_runtime(restored,config,previous,candidate)
        _runtime_health(restored,previous,changed)
        run(['systemctl','reload','proxyhub-publisher.service'])
        _wait_publication(previous)
        return
    if activation == 'publication':
        # Re-check this invariant during crash recovery as well as normal apply.
        # A publication rollback must never mask a server configuration change.
        config = json.loads((candidate / 'config.json').read_text(encoding='utf-8'))
        _publication_server(config['server'], previous, config)
        _link(previous)
        _publish(previous)
        if _publication_nginx_changed(previous,candidate):
            run(['systemctl','reload','proxyhub-nginx.service'])
        run(['systemctl', 'reload', 'proxyhub-publisher.service'])
        _wait_publication(previous)
        return
    if activation == 'web':
        config = json.loads((candidate/'config.json').read_text(encoding='utf-8'))
        _web_server(config,previous)
        restored = json.loads((previous/'config.json').read_text(encoding='utf-8'))
        _link(previous)
        _publish(previous)
        changed = _sync_runtime(restored,config,previous,candidate)
        run(['systemctl','reload','proxyhub-nginx.service'])
        run(['systemctl','reload','proxyhub-publisher.service'])
        _wait_web_health(restored,previous)
        if 'reality' in restored:
            _wait_local_reality_health(restored,previous)
        _runtime_health(restored,previous,changed)
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
        candidate_config = json.loads((candidate/'config.json').read_text(encoding='utf-8'))
        shared_topology = (candidate/'shared.json').is_file() or (previous/'shared.json').is_file()
        if shared_topology:
            _sync_runtime(config,candidate_config,previous,candidate)
        for service in _core_units(config,previous):
            if shared_topology and service in _runtime_units(config,previous):
                continue
            run(['systemctl', 'restart', service])
        # systemctl restart acknowledges process startup, not HTTPS readiness.
        # Keep the activation pending until every restored consumer is healthy.
        if 'reality' not in config:
            _wait_health(config['server'])
        if 'web' in config:
            _wait_web_health(config,previous)
        if 'reality' in config:
            _wait_local_reality_health(config,previous)
        _wait_hy2_health(config,previous)
        _wait_nodes_health(config,previous)
        _wait_publication(previous)
    else:
        # Every service is attempted even if one stop fails. The journal is left
        # pending when any action fails, so a later recover can safely retry.
        errors = []
        candidate_config = json.loads((candidate/'config.json').read_text(encoding='utf-8'))
        candidate_units = _core_units(candidate_config,candidate)
        for service in reversed(candidate_units):
            try:
                run(['systemctl', 'stop', service])
            except Exception as exc:
                errors.append(exc)
        try:
            run(['systemctl', 'disable', *candidate_units])
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


def _recovery_activation(store: StateStore, item: dict):
    """Validate durable recovery inputs before any service or deny-ledger action."""
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
    if 'previous_files' in details:
        if previous is None:
            raise OperationError('Runtime recovery is missing its previous revision')
        _verify_candidate(previous,{'files':details['previous_files']})
        if 'runtime_units_before' in details:
            before = json.loads((previous/'config.json').read_text(encoding='utf-8'))
            after = json.loads((candidate/'config.json').read_text(encoding='utf-8'))
            if (details['runtime_units_before'] != list(_runtime_units(before,previous))
                    or details.get('runtime_units_after') != list(_runtime_units(after,candidate))):
                raise OperationError('Runtime recovery service set differs from its revisions')
    if details.get('publication_safety_failed'):
        _verify_candidate(candidate,record['payload'])
    return record, details, activation, candidate, previous


def check_publication_start(store: StateStore) -> dict:
    """Read-only root service guard; incomplete revocation never opens a listener.

    It does not recover or acquire the operation lock: recover starts the service
    while holding that lock. The publisher retains its unprivileged snapshot role.
    """
    if stat.S_IMODE((ROOT/'publish').stat().st_mode) != 0o750:
        raise OperationError('Publication read gate is closed; run recovery or investigate its owner')
    security = load_security(ROOT/'publish/security.json')
    row = store.db.execute("SELECT value FROM metadata WHERE key='publication_security_generation'").fetchone()
    if row is not None and security['generation'] < int(row[0]):
        raise OperationError('Publication security generation cannot go backwards')
    pending = store.pending()
    if len(pending) > 1:
        raise OperationError('Multiple pending activations require manual investigation')
    for item in pending:
        record, details, _, candidate, previous = _recovery_activation(store,item)
        operation = record['payload'].get('subscription_operation')
        if operation and operation['kind'] in ('subscription.token.rotate','subscription.alias.retire'):
            _verify_candidate(candidate,record['payload'])
            revoked, retired = _publication_revocations(previous,candidate,operation)
            if (not {token_digest(token) for token in revoked} <= set(security['revoked_tokens'])
                    or not {alias_digest(alias['host'],alias['path_prefix']) for alias in retired}
                    <= set(security['retired_aliases'])):
                raise OperationError('Unfinished publication revocation; run deployment recover')
    return {'status':'publication_safe'}


def recover(store: StateStore) -> dict:
    """Conservatively undo an interrupted activation; never continue it."""
    with store.exclusive():
        pending = store.pending()
        if not pending:
            if stat.S_IMODE((ROOT/'publish').stat().st_mode) != 0o750:
                raise OperationError('Publication read gate has no pending owner; manual investigation required')
            return {'status': 'nothing_to_recover'}
        if len(pending) != 1:
            raise OperationError('Multiple pending activations require manual investigation')
        item = pending[0]
        if item['details'].get('publication_safety_failed'):
            # Reversible denial also protects a journal-before-gate crash whose
            # revisions were subsequently damaged. Verify before revoking.
            try:
                _publication_gate(closed=True)
            except Exception:
                run(['systemctl','stop','proxyhub-publisher.service'])
                raise
        record, details, activation, candidate, previous = _recovery_activation(store,item)
        if details.get('publication_safety_failed'):
            try:
                _revoke_publication_changes(store,previous,candidate,record['payload'].get('subscription_operation'))
            except Exception:
                # A crash before the original stop may have left the listener up.
                run(['systemctl','stop','proxyhub-publisher.service'])
                raise
            run(['systemctl','start','proxyhub-publisher.service'])
            if previous is not None:
                # The live snapshot may still be the candidate. Confirm HTTP
                # readiness before SIGHUP; _restore confirms every old profile.
                _wait_publication(previous,readiness_only=True)
        if activation in ('reality','hy2','node','runtime') or (candidate/'shared.json').exists():
            _verify_candidate(candidate,record['payload'])
            _ensure_publication_security(store)
            _restore(previous,candidate,activation,record['payload'].get('reality_operation'),
                     record['payload'].get('hy2_operation'),record['payload'].get('node_operation'))
        else:
            _ensure_publication_security(store)
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


def _publication_health(directory: Path, *, readiness_only: bool = False) -> None:
    """Check the loaded generation, exact bodies and headers through real GETs.

    A successful SIGHUP only schedules a reload. The publisher deliberately
    retains the previous valid snapshot on failure, so process health alone is
    not an acknowledgement. Requests stay on the fixed loopback publisher port;
    no URL from a browser or subscription configuration is fetched here.
    """
    bundle = validate_bundle(json.loads((directory / 'bundle.json').read_text(encoding='utf-8')))
    if bundle['generation'] != directory.name:
        raise OperationError('Publication generation differs from its immutable revision')
    try:
        security = load_security(ROOT/'publish/security.json')
    except (OSError,ValueError):
        raise OperationError('Publication security state is unavailable') from None
    def request(host, path, method, expected=None, allowed=True):
        connection = http.client.HTTPConnection('127.0.0.1', 18890, timeout=1)
        try:
            connection.request(method,path,headers={'Host':host,'Connection':'close'})
            response = connection.getresponse()
            if response.headers.get_all('X-ProxyHub-Security-Generation',[]) != [str(security['generation'])]:
                raise OperationError('Publisher has not acknowledged the safety generation')
            if response.status != (200 if allowed else 404):
                raise OperationError('Publisher route or revocation has not been acknowledged')
            if (allowed and not readiness_only
                    and response.headers.get_all('X-ProxyHub-Generation',[]) != [bundle['generation']]):
                raise OperationError('Publisher has not acknowledged the expected generation')
            if expected is None or not allowed:
                if response.headers.get_all('Content-Length',[]) != ['0'] or response.read(1):
                    raise OperationError('Publisher status or rejected route returned unexpected content')
                return
            body = expected['body'].encode('utf-8')
            if response.headers.get_all('Content-Length', []) != [str(len(body))]:
                raise OperationError('Publisher response length differs from the candidate')
            if any(response.headers.get_all(name, []) != [value]
                   for name, value in expected['headers'].items()):
                raise OperationError('Publisher response metadata differs from the candidate')
            if response.read(MAX_BODY_BYTES + 1) != (body if method=='GET' else b''):
                raise OperationError('Publisher response body differs from the candidate')
        except (OSError, http.client.HTTPException):
            # HTTP exception details may contain a credential-bearing route.
            raise OperationError('Publisher acknowledgement is unavailable') from None
        finally:
            connection.close()
    # This also confirms empty/all-revoked bundles; an empty route loop is not ACK.
    request('proxyhub.internal','/__proxyhub_publication__','HEAD')
    if readiness_only:
        return
    for profile in bundle['profiles']:
        for expected in iter_routes(profile):
            allowed = route_allowed(security,expected['host'],expected['path_prefix'],profile['token'])
            for method in ('GET','HEAD'):
                request(expected['host'],expected['path'],method,expected,allowed)


def _wait_publication(directory: Path, *, readiness_only: bool = False) -> None:
    """Bound retries: at most 15 complete profile/alias sweeps with one-second I/O waits."""
    for attempt in range(15):
        try:
            if readiness_only:
                _publication_health(directory,readiness_only=True)
            else:
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
        _subscription_transition(config,previous,payload.get('subscription_operation'))
        if activation == 'publication':
            _publication_server(server, previous, config)
        elif activation == 'reality':
            _reality_transition(config,previous,payload.get('reality_operation'))
        elif activation == 'hy2':
            _hy2_transition(config,previous,payload.get('hy2_operation'))
        elif activation == 'runtime':
            _runtime_transition(config,previous)
        elif activation == 'node':
            _nodes_transition(config,previous,payload.get('node_operation'))
        elif activation == 'web':
            _web_server(config,previous)
        else:
            _preserve_endpoints(config,previous)
        if 'web' in config:
            _check_new_ports(config,previous)
            if activation != 'publication':
                _check_new_domains(config,previous)
        _check_runtime_permissions(directory)
        _check_hy2_ports(config,previous)
        _check_node_ports(config,previous)
        if 'reality' in config:
            _check_reality_ports(config,previous)
        if activation=='reality':
            reality.probe_targets(_reality_scope(config,'primary'))
        if activation=='node':
            _probe_node_targets(config,{payload['node_operation']['node_id']})
        if previous is None:
            _check_ports(server)
        if activation=='runtime':
            _isolated_runtime_health(config,directory)
        details = {'previous':str(previous) if previous else None,
                   'candidate':str(directory),'activation':activation}
        if (directory/'shared.json').is_file() or (previous is not None and (previous/'shared.json').is_file()):
            old = json.loads((previous/'config.json').read_text(encoding='utf-8')) if previous else {}
            details.update(runtime_units_before=list(_runtime_units(old,previous)),
                           runtime_units_after=list(_runtime_units(config,directory)))
            if previous is not None:
                details['previous_files'] = {path.relative_to(previous).as_posix():digest(path.read_bytes())
                    for path in previous.rglob('*') if path.is_file()}
        operation = payload.get('subscription_operation')
        safety_change = operation is not None and operation['kind'] in (
            'subscription.token.rotate','subscription.alias.retire')
        if stat.S_IMODE((ROOT/'publish').stat().st_mode) != 0o750:
            raise OperationError('Publication read gate is closed; run recovery or investigate its owner')
        if safety_change:
            # This is a recoverable safety intent, including the policy-write
            # failure and service-stop boundaries. It must precede both actions.
            details['publication_safety_failed'] = True
            if previous is not None and 'previous_files' not in details:
                details['previous_files'] = {path.relative_to(previous).as_posix():digest(path.read_bytes())
                    for path in previous.rglob('*') if path.is_file()}
            store.record(identity,'activating',details)
            try:
                _publication_gate(closed=True)
                _recovery_activation(store,{'plan_id':identity,'details':details})
                _revoke_publication_changes(store,previous,directory,operation)
                # The publisher may have been stopped; confirm readiness before
                # SIGHUP. Only this journal's durable revocation opens its gate.
                run(['systemctl','start','proxyhub-publisher.service'])
                if previous is not None:
                    _wait_publication(previous,readiness_only=True)
            except Exception:
                # A failed policy write cannot safely restore the old route.
                # Stop publication until recover has durably completed denial.
                run(['systemctl','stop','proxyhub-publisher.service'])
                raise OperationError('Publication safety activation failed; publisher is closed until recovery') from None
        else:
            store.record(identity,'activating',details)
        try:
            if not safety_change:
                _revoke_publication_changes(store,previous,directory)
            _link(directory)
            _publish(directory)
            if activation == 'publication':
                if _publication_nginx_changed(directory,previous):
                    run(['systemctl','reload','proxyhub-nginx.service'])
                run(['systemctl', 'reload', 'proxyhub-publisher.service'])
                _wait_publication(directory)
            elif activation in ('hy2','node','runtime'):
                old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
                changed = _sync_runtime(config,old,directory,previous)
                _runtime_health(config,directory,changed)
                run(['systemctl','reload','proxyhub-publisher.service'])
                _wait_publication(directory)
            elif activation == 'web':
                old = json.loads((previous/'config.json').read_text(encoding='utf-8'))
                changed = _sync_runtime(config,old,directory,previous)
                run(['systemctl','reload','proxyhub-nginx.service'])
                run(['systemctl','reload','proxyhub-publisher.service'])
                _wait_web_health(config,directory)
                if 'reality' in config:
                    _wait_local_reality_health(config,directory)
                _runtime_health(config,directory,changed)
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
                shared_topology = (directory/'shared.json').is_file() or (previous is not None and (previous/'shared.json').is_file())
                if shared_topology:
                    old = json.loads((previous/'config.json').read_text(encoding='utf-8')) if previous else {}
                    _sync_runtime(config,old,directory,previous)
                for service in _core_units(config,directory):
                    if shared_topology and service in _runtime_units(config,directory):
                        continue
                    run(['systemctl','restart',service])
                if 'reality' not in config:
                    _wait_health(server)
                if 'web' in config:
                    _wait_web_health(config,directory)
                elif payload.get('website_change') or config.get('website_content'):
                    _website_health(server, directory)
                if 'reality' in config:
                    _wait_local_reality_health(config,directory)
                _wait_hy2_health(config,directory)
                _wait_nodes_health(config,directory)
                _wait_publication(directory)
                run(['systemctl','enable',*_core_units(config,directory)])
            store.commit(identity)
        except Exception:
            # A successful database COMMIT is the boundary. Never undo it if a
            # later Python exception occurs; recovery sees no pending activation.
            if store.inspect(identity)['status'] != 'applied':
                if 'previous_files' in details:
                    _verify_candidate(previous,{'files':details['previous_files']})
                if activation in ('reality','hy2','node','runtime'):
                    _restore(previous,directory,activation,payload.get('reality_operation'),payload.get('hy2_operation'),payload.get('node_operation'))
                else:
                    _restore(previous, directory, activation)
                store.abort(identity, {'previous_available': previous is not None})
            raise
        return {'status':'applied','revision':payload['revision'],'activation':activation,
                'runtime_health':'passed','real_client_acceptance':'required'}
