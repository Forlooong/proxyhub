"""Management contracts, secret projections, stable snapshots and renewal audit."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from proxyhub import certificates, deployment, management, management_read, site
from proxyhub.core import OperationError, canonical
from proxyhub.host import ssh
from proxyhub.management_errors import ManagementError
from proxyhub.jobs import Jobs
from proxyhub.proxy import validate_spec
from proxyhub.state import StateStore


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    directory = root / 'revisions' / ('a' * 32)
    directory.mkdir(parents=True)
    (root / 'certificates').mkdir()
    (root / 'panel').mkdir()
    server = validate_spec({
        'site_domain': 'site.example.test', 'subscription_domain': 'sub.example.test',
        'public_address': '203.0.113.10', 'uuid': 'a875d3ac-980f-4a0c-929e-78cc678429f1',
        'private_key': 'A' * 43, 'public_key': 'B' * 42 + 'A', 'short_id': '9988776655443322',
        'certificate_path': '/var/lib/proxyhub/certificates/0123456789abcdef/fullchain.pem',
        'key_path': '/var/lib/proxyhub/certificates/0123456789abcdef/privkey.pem',
    })
    config = {'server': server, 'subscription': {'id': 'default', 'name': 'Fixture',
              'token': 'subscription-secret-test-fixture-123456', 'userinfo': {}, 'update_interval_hours': 6},
              'overrides': {'mixed-port': 17890}}
    (directory / 'config.json').write_bytes(canonical(config))
    (directory / 'client.yaml').write_text('uuid: '+server['uuid'],encoding='utf-8')
    from proxyhub.publication import build_bundle, save_bundle, save_security
    import base64
    (root/'publish').mkdir()
    save_security(root/'publish/security.json',{'schema_version':1,'generation':0,'revoked_tokens':[],'retired_aliases':[]})
    save_bundle(directory/'bundle.json',build_bundle([{
        **config['subscription'],'host':server['subscription_domain'],
        'yaml':'uuid: '+server['uuid'], 'raw':base64.b64encode(b'vless://fixture').decode('ascii'),
    }],directory.name))
    pointer = {'current': directory}
    locking = {'active': False}
    @contextmanager
    def exclusive(store):
        assert not locking['active']
        locking['active'] = True
        try:
            yield
        finally:
            locking['active'] = False
    monkeypatch.setattr(StateStore, 'exclusive', exclusive)
    monkeypatch.setattr(deployment, 'ROOT', root)
    monkeypatch.setattr(deployment, '_current_revision', lambda: pointer['current'])
    monkeypatch.setattr(management_read, 'accounts', lambda: ['root'])
    monkeypatch.setattr(management_read, 'run', lambda *a, **kw: 'active\n')
    monkeypatch.setattr(site, 'validate_certificate', lambda *a: {
        'domains': ['site.example.test','sub.example.test'], 'not_after': '2030-01-01T00:00:00+00:00',
        'sha256': 'c' * 64,
    })
    monkeypatch.setattr(certificates, 'managed_acme', lambda server: True)
    service = management.OperationService(root)
    prepared = []
    def prepare(store, candidate, **kwargs):
        prepared.append((deepcopy(candidate),kwargs))
        identity = store.plan('deployment',{'revision':'b'*32},expected_revision=kwargs.get('expected_revision'))
        return {'plan_id':identity}
    monkeypatch.setattr(deployment, 'prepare', prepare)
    yield SimpleNamespace(root=root, directory=directory, config=config, pointer=pointer,
                          locking=locking, service=service, prepared=prepared)
    service.close()


def finish_job(service, operation, payload):
    submitted = service.handle(operation,payload)
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        job = service.handle('job.get',submitted)
        if job['status'] not in ('queued','running'):
            return job
        time.sleep(.01)
    pytest.fail('Management job did not complete')


def assert_private_fields_absent(value, config):
    encoded = json.dumps(value,ensure_ascii=False)
    for secret in (config['server']['private_key'],config['server']['uuid'],
                   config['server']['short_id'],config['subscription']['token']):
        assert secret not in encoded


@pytest.mark.parametrize('kind', ['reality.migrate','reality.select','reality.retire'])
def test_reality_operations_share_typed_plans_without_exposing_credentials(environment,kind):
    from proxyhub import reality
    from proxyhub.websites import normalize_web
    env=environment
    config=deepcopy(env.config)
    config['web']=normalize_web(config)
    config['web']['sites'][0]['domains'].append('next.example.test')
    config['web']['certificates'][0]['domains'].append('next.example.test')
    values={'type':'site','site_id':'default','sni':'next.example.test'}
    if kind!='reality.migrate':
        config=reality.change(config,'reality.migrate',values)
        endpoint=config['reality']['primary_endpoint_id']
        values={'endpoint_id':'default' if kind=='reality.select' else endpoint}
        if kind=='reality.retire':config=reality.change(config,'reality.select',{'endpoint_id':'default'})
    (env.directory/'config.json').write_bytes(canonical(config))
    result=finish_job(env.service,'plan.create',{'kind':kind,'base_revision':0,'values':values})
    assert result['status']=='succeeded'
    assert_private_fields_absent(result,config)
    candidate,options=env.prepared[0]
    assert options['reality_operation']['kind']==kind.split('.')[1]
    assert all(candidate[key]==config[key] for key in ('server','web','subscription','overrides'))
    assert options['reality_operation']['endpoint_id']==(
        candidate['reality']['primary_endpoint_id'] if kind=='reality.migrate' else values['endpoint_id'])
    resources=env.service.handle('reality.resources',{})
    assert resources['revision']==0
    assert_private_fields_absent(resources,config)


def test_overview_reports_selected_sni_without_rewriting_original_website(environment):
    from proxyhub import reality
    from proxyhub.websites import normalize_web
    env=environment
    config=deepcopy(env.config)
    config['web']=normalize_web(config)
    config['web']['sites'][0]['domains'].append('next.example.test')
    config['web']['certificates'][0]['domains'].append('next.example.test')
    config=reality.change(config,'reality.migrate',{'type':'site','site_id':'default','sni':'next.example.test'})
    (env.directory/'config.json').write_bytes(canonical(config))
    result=env.service.handle('overview',{})
    assert result['proxy']['sni']=='next.example.test'
    assert result['website']['domain']==config['server']['site_domain']=='site.example.test'
    assert len(result['reality']['endpoints'])==2
    assert len([c for c in result['certificates'][0]['consumers'] if c['kind']=='reality'])==2
    assert_private_fields_absent(result,config)


@pytest.mark.parametrize('kind', ['hy2.create','hy2.update','hy2.enable','hy2.disable'])
def test_hy2_operations_prepare_restricted_plans_without_passwords(environment,kind):
    from proxyhub import hy2
    env = environment
    config = deepcopy(env.config)
    values = {'server_name':'site.example.test','certificate_id':'default'}
    if kind != 'hy2.create':
        config = hy2.change(config,'hy2.create',values)
        if kind == 'hy2.enable':
            config = hy2.change(config,'hy2.disable',{})
        values = {'name':'Updated HY2'} if kind == 'hy2.update' else {}
    (env.directory/'config.json').write_bytes(canonical(config))
    job = finish_job(env.service,'plan.create',{'kind':kind,'base_revision':0,'values':values})
    assert job['status'] == 'succeeded'
    candidate, options = env.prepared[0]
    assert options == {'expected_revision':0,'hy2_operation':{'kind':kind.split('.')[1]}}
    assert all(candidate[key] == config[key] for key in ('server','subscription','overrides'))
    password = candidate['hy2']['password']
    assert password not in json.dumps(job) and 'password' not in json.dumps(job)
    assert_private_fields_absent(job,config)
    resources = env.service.handle('hy2.resources',{})
    assert resources['revision'] == 0
    assert 'password' not in json.dumps(resources) and password not in json.dumps(resources)


def test_hy2_overview_reports_binding_and_actual_service_state(environment):
    from proxyhub import hy2
    env = environment
    config = hy2.change(env.config,'hy2.create',{'server_name':'site.example.test','certificate_id':'default'})
    (env.directory/'config.json').write_bytes(canonical(config))
    result = env.service.handle('overview',{})
    assert result['hy2']['node']['id'] == 'local-hy2'
    assert {'name':'proxyhub-hy2','status':'active'} in result['services']
    assert any(item['kind'] == 'hy2' for item in result['certificates'][0]['consumers'])
    assert result['proxy']['id'] == 'local-reality'
    assert config['hy2']['password'] not in json.dumps(result) and 'password' not in json.dumps(result)
    assert_private_fields_absent(result,config)


@pytest.mark.parametrize('kind',['nodes.create','nodes.update','nodes.enable','nodes.disable','nodes.delete'])
def test_protocol_node_plans_scope_one_node_and_never_return_credentials(environment,kind):
    from proxyhub import nodes
    env = environment
    config = deepcopy(env.config)
    values = {'protocol':'ss','name':'Independent SS','port':5443,'options':{'method':'aes-256-gcm'}}
    if kind != 'nodes.create':
        config = nodes.change(config,'nodes.create',values)
        identity = config['nodes'][0]['id']
        if kind in {'nodes.enable','nodes.delete'}:
            config = nodes.change(config,'nodes.disable',{'node_id':identity})
        values = {'node_id':identity,**({'name':'Renamed SS'} if kind == 'nodes.update' else {})}
    (env.directory/'config.json').write_bytes(canonical(config))
    job = finish_job(env.service,'plan.create',{'kind':kind,'base_revision':0,'values':values})
    assert job['status'] == 'succeeded'
    candidate, options = env.prepared[0]
    identity = candidate['nodes'][0]['id'] if kind == 'nodes.create' else values['node_id']
    assert options == {'expected_revision':0,'node_operation':{'kind':kind.split('.')[1],'node_id':identity}}
    assert all(candidate[key] == config[key] for key in ('server','subscription','overrides'))
    resources = env.service.handle('nodes.resources',{})
    assert resources['revision'] == 0
    encoded = json.dumps([job,resources])
    assert 'credentials' not in encoded
    for selected in candidate.get('nodes',[]) + config.get('nodes',[]):
        for secret in selected['credentials'].values():
            assert secret not in encoded
    assert_private_fields_absent([job,resources],config)


def test_protocol_node_overview_reports_service_and_certificate_consumer(environment):
    from proxyhub import nodes
    env = environment
    config = nodes.change(env.config,'nodes.create',{'protocol':'tuic','name':'TUIC','port':5443,
        'options':{'sni':'site.example.test','certificate_id':'default'}})
    (env.directory/'config.json').write_bytes(canonical(config))
    result = env.service.handle('overview',{})
    identity = config['nodes'][0]['id']
    assert result['nodes']['nodes'][0]['id'] == identity
    assert {'name':'proxyhub-node@'+identity,'status':'active'} in result['services']
    assert any(item['id'] == identity for item in result['certificates'][0]['consumers'])
    assert 'credentials' not in json.dumps(result)
    for secret in config['nodes'][0]['credentials'].values():
        assert secret not in json.dumps(result)


@pytest.mark.parametrize('missing_hy2', [False, True])
def test_shared_overview_reports_one_actual_unit_and_separate_inbound_states(environment,missing_hy2):
    from proxyhub import hy2, nodes
    env = environment
    config = hy2.change(env.config,'hy2.create',{'server_name':'site.example.test','certificate_id':'default'})
    for port in (5443,5444):
        config = nodes.change(config,'nodes.create',{'protocol':'ss','name':'SS '+str(port),'port':port,
            'options':{'method':'aes-256-gcm'}})
    config = nodes.change(config,'nodes.disable',{'node_id':config['nodes'][1]['id']})
    (env.directory/'config.json').write_bytes(canonical(config))
    inbounds = [{'tag':config['nodes'][0]['listener_id']}]
    if not missing_hy2:
        inbounds.append({'tag':config['hy2']['listener_id']})
    (env.directory/'shared.json').write_bytes(canonical({'inbounds':inbounds}))
    result = env.service.handle('overview',{})
    assert {'name':'proxyhub-shared','status':'active'} in result['services']
    assert not any(item['name']=='proxyhub-hy2' or item['name'].startswith('proxyhub-node@')
                   for item in result['services'])
    assert result['hy2']['node']['runtime_status'] == ('not-loaded' if missing_hy2 else 'active')
    assert [node['runtime_status'] for node in result['nodes']['nodes']] == ['active','inactive']
    assert all(node['runtime_group']=='proxyhub-shared' for node in result['nodes']['nodes'])
    assert 'credentials' not in json.dumps(result)
    assert config['hy2']['password'] not in json.dumps(result)


@pytest.mark.parametrize(('changed','reconnect'), [(False,False),(True,False),(True,True)])
def test_node_plan_previews_exact_shared_group_impact(environment,monkeypatch,changed,reconnect):
    env = environment
    original = deployment.prepare
    def prepare(*args,**options):
        result = original(*args,**options)
        result['shared_runtime'] = {'changed':changed,'reconnect_required':reconnect,
            'members':[{'id':'local-hy2','name':'Existing HY2','protocol':'hy2'},
                       {'id':'c'*32,'name':'Existing TUIC','protocol':'tuic'}]}
        return result
    monkeypatch.setattr(deployment,'prepare',prepare)
    preview = env.service.create_plan('nodes.create',0,{'protocol':'ss','name':'New SS','port':5443,
        'options':{'method':'aes-256-gcm'}})
    impact = ' '.join(preview['impact'])
    if changed:
        assert 'Existing HY2 (HY2)' in impact and 'Existing TUIC (TUIC)' in impact
        assert ('均需重连' in impact) is reconnect
    else:
        assert '运行配置未变化' in impact and '连接保持' in impact
    assert 'REALITY 核心和网站进程保持运行' in impact
    assert_private_fields_absent(preview,env.config)


def test_website_upload_and_restore_share_existing_plan_contract(environment,monkeypatch):
    import base64
    import io
    import zipfile
    env = environment
    archive = io.BytesIO()
    with zipfile.ZipFile(archive,'w') as output:
        output.writestr('index.html','<h1>Upload</h1>')
    result = env.service.create_plan('website.content.upload',0,{'archive_base64':base64.b64encode(archive.getvalue()).decode()})
    config, options = env.prepared[-1]
    assert options['site_files'] == {'index.html':b'<h1>Upload</h1>'}
    assert options['website_change'] is True and options['publish_only'] is False
    assert_private_fields_absent(result,env.config)
    monkeypatch.setattr(deployment,'content_version',lambda store,version,site_id='default':{'index.html':b'old'})
    env.service.create_plan('website.content.restore',0,{'version_id':'b'*32})
    restored,options = env.prepared[-1]
    assert restored['server'] == env.config['server'] and restored['subscription'] == env.config['subscription']
    assert restored['website_content'] == 'b'*32 and options['site_files'] is None
    env.config['website_content'] = 'b'*32
    (env.directory/'config.json').write_bytes(canonical(env.config))
    env.service.create_plan('website.content.restore',0,{'version_id':'template'})
    assert 'website_content' not in env.prepared[-1][0]
    with pytest.raises(ManagementError,match='HTML'):
        env.service.create_plan('website.update',0,{'site_title':'Cannot change uploaded HTML'})


def test_website_preview_rejects_stale_and_unrelated_plans(environment):
    from proxyhub.core import digest
    env = environment
    (env.directory/'site').mkdir()
    body = b'<h1>Hello</h1><script>secretScript()</script>'
    (env.directory/'site/index.html').write_bytes(body)
    with env.service.state() as store:
        identity = store.plan('deployment',{'revision':env.directory.name,'website_change':True,
                                          'files':{'site/index.html':digest(body)}})
        unrelated = store.plan('ssh-append',{})
    result = env.service.handle('website.content.preview',{'plan_id':identity})
    assert 'Hello' in result['html'] and '<script>' not in result['html']
    assert result['file_count'] == 1
    assert_private_fields_absent(result,env.config)
    with pytest.raises(ManagementError):
        env.service.handle('website.content.preview',{'plan_id':unrelated})
    with env.service.state() as store:
        store.commit(unrelated)
    with pytest.raises(ManagementError) as error:
        env.service.handle('website.content.preview',{'plan_id':identity})
    assert error.value.code == 'STALE_REVISION'


def test_oversized_website_preview_explains_that_apply_remains_available(environment,monkeypatch):
    from proxyhub import site_content
    from proxyhub.core import digest
    env = environment
    (env.directory/'site').mkdir()
    (env.directory/'site/index.html').write_bytes(b'page')
    with env.service.state() as store:
        identity = store.plan('deployment',{'revision':env.directory.name,'website_change':True,
                                          'files':{'site/index.html':digest(b'page')}})
    def too_large(files):
        raise ValueError('private content')
    monkeypatch.setattr(site_content,'preview_html',too_large)
    with pytest.raises(ManagementError) as error:
        env.service.handle('website.content.preview',{'plan_id':identity})
    assert error.value.code == 'PREVIEW_UNAVAILABLE' and '仍可' in str(error.value)
    assert 'private content' not in str(error.value)


def test_synchronous_website_service_does_not_recover_manager_jobs(environment,monkeypatch):
    def forbidden(*args):
        pytest.fail('Synchronous CLI must not open the manager job queue')
    monkeypatch.setattr(management,'Jobs',forbidden)
    service = management.OperationService(environment.root,synchronous=True)
    assert service.jobs is None
    service.close()


def test_new_certificate_issue_is_planned_before_acme_and_commits_stable_binding(environment,monkeypatch):
    from proxyhub import management_web
    env = environment
    seen = []
    monkeypatch.setattr(management_web,'check_domain',lambda server,domain:None)
    def issued(domains,**options):
        seen.append((domains,options))
        return {'certificate_path':'/var/lib/proxyhub/certificates/'+'d'*64+'/fullchain.pem',
                'key_path':'/var/lib/proxyhub/certificates/'+'d'*64+'/privkey.pem'}
    monkeypatch.setattr(certificates,'issue',issued)
    preview = env.service.create_plan('certificate.issue',0,{'name':'Second certificate','domains':['second.example.test']})
    assert seen == []
    with env.service.state() as store:
        payload = store.inspect(preview['plan_id'])['payload']
    assert payload['lineage'] == 'proxyhub-'+payload['certificate_id']
    monkeypatch.setattr(deployment,'apply',fake_activation)
    env.service.apply_plan(preview['plan_id'])
    config,options = env.prepared[-1]
    assert options['web_only'] is True
    assert config['web']['certificates'][-1]['id'] == payload['certificate_id']
    assert config['server'] == env.config['server']
    assert seen == [(['second.example.test'],{'standalone':False,'lineage':payload['lineage']})]
    with env.service.state() as store:
        assert store.inspect(preview['plan_id'])['status'] == 'applied' and store.revision == 1


def test_scoped_renewal_changes_only_its_binding(environment,monkeypatch):
    from proxyhub import management_web
    from proxyhub.websites import normalize_web
    env = environment
    config = deepcopy(env.config)
    config['web'] = normalize_web(config)
    identity = 'c'*32
    config['web']['certificates'].append({'id':identity,'name':'Separate certificate','domains':['second.example.test'],
        'certificate_path':'/var/lib/proxyhub/certificates/'+'d'*64+'/fullchain.pem',
        'key_path':'/var/lib/proxyhub/certificates/'+'d'*64+'/privkey.pem'})
    (env.directory/'config.json').write_bytes(canonical(config))
    monkeypatch.setattr(management_web,'check_domain',lambda server,domain:None)
    monkeypatch.setattr(certificates,'acme_source',lambda binding:{'lineage':'proxyhub-'+identity,'domains':['second.example.test']})
    monkeypatch.setattr(certificates,'issue',lambda *args,**kw:{
        'certificate_path':'/var/lib/proxyhub/certificates/'+'e'*64+'/fullchain.pem',
        'key_path':'/var/lib/proxyhub/certificates/'+'e'*64+'/privkey.pem'})
    monkeypatch.setattr(deployment,'apply',fake_activation)
    preview = env.service.create_plan('certificate.renew',0,{'certificate_id':identity})
    env.service.apply_plan(preview['plan_id'])
    changed, options = env.prepared[-1]
    assert changed['web']['certificates'][0] == config['web']['certificates'][0]
    assert changed['web']['certificates'][1]['id'] == identity
    assert changed['server'] == config['server'] and changed['subscription'] == config['subscription']
    assert options['web_only'] is True


def test_new_acme_plan_rejects_configuration_changed_during_issue(environment,monkeypatch):
    from proxyhub import management_web
    env = environment
    monkeypatch.setattr(management_web,'check_domain',lambda server,domain:None)
    preview = env.service.create_plan('certificate.issue',0,{'name':'Second certificate','domains':['second.example.test']})
    def changed(*args,**kwargs):
        with env.service.state() as store:
            store.commit(store.plan('ssh-append',{}))
        return {'certificate_path':'unused','key_path':'unused'}
    monkeypatch.setattr(certificates,'issue',changed)
    with pytest.raises(ManagementError) as error:
        env.service.apply_plan(preview['plan_id'])
    assert error.value.code == 'STALE_REVISION' and env.prepared == []


def test_public_web_resources_have_stable_ids_and_no_secret_material(environment):
    env = environment
    result = env.service.handle('web.resources',{})
    assert result['websites'][0]['id'] == 'default'
    assert result['websites'][0]['listener_id'] == 'site-default'
    assert result['certificates'][0]['id'] == 'default'
    assert {item['kind'] for item in result['certificates'][0]['consumers']} == {'website','subscription','reality'}
    assert_private_fields_absent(result,env.config)
    assert 'key_path' not in json.dumps(result)


def test_overview_projects_only_public_state(environment):
    env = environment
    result = env.service.handle('overview',{})
    assert result['revision'] == 0 and result['certificate']['status'] == 'valid'
    assert len(result['services']) == 6 and result['operation_pending'] is False
    assert_private_fields_absent(result,env.config)


def test_certificate_failure_preserves_overview_and_repair_actions(environment, monkeypatch):
    env = environment
    secret_error = 'expired certificate private key secret should not escape'
    def invalid(*args):
        raise ValueError(secret_error)
    monkeypatch.setattr(site,'validate_certificate',invalid)
    result = env.service.handle('overview',{})
    assert result['revision'] == 0
    assert result['certificate']['status'] == 'invalid'
    assert result['certificate']['sha256'] is None and result['certificate']['not_after'] is None
    assert result['certificate']['automatic_renewal'] is True
    assert result['website']['domain'] == env.config['server']['site_domain']
    assert secret_error not in json.dumps(result)
    preview = env.service.create_plan('certificate.renew',0,{})
    assert preview['kind'] == 'certificate.renew'


def test_overview_copies_config_under_lock_but_probes_after_release(environment, monkeypatch):
    env = environment
    original_active, original_overview = env.service.active, management.overview
    def active():
        assert env.locking['active']
        return original_active()
    def projected(*args):
        assert not env.locking['active']
        return original_overview(*args)
    monkeypatch.setattr(env.service,'active',active)
    monkeypatch.setattr(management,'overview',projected)
    env.service.handle('overview',{})
    assert not env.locking['active']


def pending_activation(env):
    candidate = env.root/'revisions'/('b'*32)
    candidate.mkdir()
    config = deepcopy(env.config)
    config['server']['site_title'] = 'Uncommitted title'
    (candidate/'config.json').write_bytes(canonical(config))
    with env.service.state() as store:
        identity = store.plan('deployment',{'revision':candidate.name})
        store.record(identity,'activating',{'previous':str(env.directory),'candidate':str(candidate)})
    env.pointer['current'] = candidate


def test_pending_overview_shows_last_committed_config_and_reveal_is_blocked(environment):
    env = environment
    pending_activation(env)
    result = env.service.handle('overview',{})
    assert result['operation_pending'] is True and result['revision'] == 0
    assert result['website']['title'] == env.config['server']['site_title']
    for format_name in ('links','yaml'):
        with pytest.raises(ManagementError) as error:
            env.service.handle('subscription.reveal',{'format':format_name})
        assert error.value.code == 'BUSY'


def test_pending_overview_reads_committed_runtime_layout(environment):
    from proxyhub import hy2
    env = environment
    env.config = hy2.change(env.config,'hy2.create',{'server_name':'site.example.test','certificate_id':'default'})
    (env.directory/'config.json').write_bytes(canonical(env.config))
    (env.directory/'shared.json').write_bytes(canonical({'inbounds':[{'tag':env.config['hy2']['listener_id']}]}))
    pending_activation(env)
    (env.pointer['current']/'shared.json').write_bytes(canonical({'inbounds':[]}))
    result = env.service.handle('overview',{})
    assert result['operation_pending'] is True
    assert result['hy2']['node']['runtime_group'] == 'proxyhub-shared'
    assert result['hy2']['node']['runtime_status'] == 'active'


def test_empty_or_invalid_shared_runtime_never_reports_retained_disabled_hy2_active(environment):
    from proxyhub import hy2
    env = environment
    config = hy2.change(env.config,'hy2.create',{'server_name':'site.example.test','certificate_id':'default'})
    config = hy2.change(config,'hy2.disable',{})
    (env.directory/'config.json').write_bytes(canonical(config))
    for body in (canonical({'inbounds':[]}),b'not valid JSON'):
        (env.directory/'shared.json').write_bytes(body)
        result = env.service.handle('overview',{})
        assert result['hy2']['node']['runtime_status'] == 'inactive'
        assert result['hy2']['node']['runtime_group'] == 'proxyhub-shared'
        assert not any(item['name']=='proxyhub-hy2' for item in result['services'])


def test_only_explicit_reveal_returns_link_or_node_credential(environment):
    env = environment
    result = env.service.handle('subscription.reveal',{'format':'links'})
    assert env.config['subscription']['token'] in result['links']['mihomo']
    assert env.config['server']['uuid'] in env.service.handle('subscription.reveal',{'format':'yaml'})['yaml']
    assert_private_fields_absent(env.service.handle('jobs.list',{}),env.config)


@pytest.mark.parametrize('operation,payload',[
    ('shell.exec',{'command':'id'}),('overview',{'path':'/etc/shadow'}),
    ('plan.apply',{'plan_id':'../outside'}),('subscription.reveal',{'format':'private_key'}),
    ('plan.create',{'kind':'website.update','base_revision':True,'values':{'site_title':'Changed'}}),
])
def test_unknown_operations_fields_and_identifiers_fail_closed(environment,operation,payload):
    with pytest.raises(ManagementError):
        environment.service.handle(operation,payload)


def test_stale_plan_revision_rejected_before_preparation(environment):
    env = environment
    with env.service.state() as store:
        store.commit(store.plan('ssh-append',{}))
    job = finish_job(env.service,'plan.create',{'kind':'website.update','base_revision':0,'values':{'site_title':'Changed'}})
    assert job['status'] == 'failed' and job['error']['code'] == 'STALE_REVISION'
    assert env.prepared == []


def test_subscription_update_uses_publication_mode_and_no_secret_preview(environment):
    env = environment
    values = {'name':'Updated','update_interval_hours':12,'userinfo':{'upload':1,'download':2,'total':100},
              'reset_day':3,'reset_at':None,'overrides':{'mixed-port':17891}}
    job = finish_job(env.service,'plan.create',{'kind':'subscription.update','base_revision':0,'values':values})
    assert job['status'] == 'succeeded'
    candidate, arguments = env.prepared[0]
    assert arguments == {'expected_revision':0,'publish_only':True,
                         'subscription_operation':{'kind':'subscription.update','subscription_id':'default'}}
    assert candidate['server'] == env.config['server']
    assert candidate['subscription']['token'] == env.config['subscription']['token']
    assert candidate['overrides'] == values['overrides']
    assert_private_fields_absent(job,env.config)


@pytest.mark.parametrize(('kind','values'),[
    ('subscription.create',{'name':'Family','node_ids':['local-reality']}),
    ('subscription.copy',{'subscription_id':'default','name':'Copy'}),
    ('subscription.update',{'subscription_id':'default','path_prefix':'/family','formats':['yaml']}),
    ('subscription.enable',{'subscription_id':'default'}),
    ('subscription.disable',{'subscription_id':'default'}),
    ('subscription.alias.add',{'subscription_id':'default','host':'sub.example.test','path_prefix':'/alias'}),
    ('subscription.alias.retire',{'subscription_id':'default','alias_id':'alias-1'}),
    ('subscription.token.rotate',{'subscription_id':'default'}),
])
def test_subscription_lifecycle_uses_one_publication_plan_and_secret_free_preview(environment,kind,values):
    from proxyhub import subscriptions
    env = environment
    if kind == 'subscription.enable':
        env.config['subscription']['enabled'] = False
    if kind == 'subscription.alias.retire':
        env.config['subscription']['aliases'] = [{'id':'alias-1','host':'sub.example.test','path_prefix':'/alias'}]
    (env.directory/'config.json').write_bytes(canonical(env.config))
    job = finish_job(env.service,'plan.create',{'kind':kind,'base_revision':0,'values':values})
    assert job['status'] == 'succeeded', job
    candidate, arguments = env.prepared[0]
    assert arguments == {'expected_revision':0,'publish_only':True,
                         'subscription_operation':{'kind':kind,'subscription_id':next(
                             profile['id'] for profile in subscriptions.normalize(candidate)
                             if profile['id'] not in {item['id'] for item in subscriptions.normalize(env.config)})
                             if kind in {'subscription.create','subscription.copy'} else values.get('subscription_id','default')}}
    assert candidate['server'] == env.config['server']
    for profile in subscriptions.normalize(candidate):
        assert profile['token'] not in json.dumps(job)
    assert_private_fields_absent(job,env.config)
    assert '节点凭据' in ''.join(job['result']['warnings'])


@pytest.mark.parametrize('security_state',['missing','invalid'])
def test_subscription_unknown_security_is_visible_and_reveal_fails_closed(environment,security_state):
    env = environment
    path = env.root/'publish/security.json'
    if security_state == 'missing':
        path.unlink()
    else:
        path.write_text('not valid security',encoding='utf-8')
    resources = env.service.handle('subscription.resources',{})
    assert resources['profiles'][0]['access_status'] == 'unknown'
    assert_private_fields_absent(resources,env.config)
    for format_name in ('links','yaml'):
        with pytest.raises(ManagementError) as error:
            env.service.handle('subscription.reveal',{'format':format_name})
        assert error.value.code == 'SUBSCRIPTION_BLOCKED'


def test_subscription_revoked_token_remains_blocked_for_primary_and_all_aliases(environment):
    from proxyhub.publication import advance_security
    env = environment
    env.config['subscription']['aliases'] = [{'id':'alias-1','host':'sub.example.test','path_prefix':'/alias'}]
    (env.directory/'config.json').write_bytes(canonical(env.config))
    advance_security(env.root/'publish/security.json',tokens=[env.config['subscription']['token']])
    resources = env.service.handle('subscription.resources',{})
    assert resources['profiles'][0]['access_status'] == 'blocked'
    assert resources['profiles'][0]['aliases'][0]['access_status'] == 'blocked'
    with pytest.raises(ManagementError,match='节点|令牌|安全'):
        env.service.handle('subscription.reveal',{'format':'links'})


def test_subscription_reveal_selects_one_published_profile_and_enabled_formats(environment):
    import base64
    from proxyhub import subscriptions
    from proxyhub.publication import build_bundle, save_bundle
    env = environment
    config = subscriptions.change(env.config,'subscription.create',{
        'name':'YAML only','node_ids':['local-reality'],'path_prefix':'/family','formats':['yaml'],
    })
    extra = config['subscriptions'][0]
    (env.directory/'config.json').write_bytes(canonical(config))
    save_bundle(env.directory/'bundle.json',build_bundle([
        {**{key:value for key,value in profile.items() if key not in {'node_ids','overrides'}},
         'yaml':'profile: '+profile['id'],'raw':base64.b64encode(b'vless://fixture').decode('ascii')}
        for profile in subscriptions.normalize(config)
    ],env.directory.name))
    old_links = env.service.handle('subscription.reveal',{'format':'links'})['links']
    assert old_links == {'mihomo':'https://sub.example.test/sub/'+env.config['subscription']['token']+'/mihomo.yaml',
                         'raw':'https://sub.example.test/sub/'+env.config['subscription']['token']+'/raw'}
    result = env.service.handle('subscription.reveal',{'format':'links','subscription_id':extra['id']})
    assert set(result['links']) == {'mihomo'}
    assert '/family/'+extra['token']+'/' in result['links']['mihomo']
    assert env.service.handle('subscription.reveal',{'format':'yaml','subscription_id':extra['id']}) == {'yaml':'profile: '+extra['id']}
    projected = env.service.handle('overview',{})['subscriptions']
    assert len(projected['profiles']) == 2
    assert extra['token'] not in json.dumps(projected)


def test_subscription_links_exclude_retired_alias_and_gui_uri_keeps_url_last(environment):
    from urllib.parse import parse_qs, urlsplit
    from proxyhub.publication import advance_security
    env = environment
    env.config['subscription']['name'] = 'Family & devices'
    env.config['subscription']['aliases'] = [
        {'id':'retired','host':'sub.example.test','path_prefix':'/old'},
        {'id':'retained','host':'sub.example.test','path_prefix':'/kept'},
    ]
    (env.directory/'config.json').write_bytes(canonical(env.config))
    advance_security(env.root/'publish/security.json',aliases=[env.config['subscription']['aliases'][0]])
    result = env.service.handle('subscription.reveal',{'format':'links'})
    assert [alias['id'] for alias in result['aliases']] == ['retained']
    assert result['aliases'][0]['links']['mihomo'].endswith('/kept/'+env.config['subscription']['token']+'/mihomo.yaml')
    uri = next(iter(result['imports'].values()))
    assert uri.startswith('clash-verge://install-config?name=')
    assert uri.count('&') == 1 and '&url=' in uri
    assert parse_qs(urlsplit(uri).query) == {'name':['Family & devices'],'url':[result['links']['mihomo']]}
    resources = env.service.handle('subscription.resources',{})
    assert resources['profiles'][0]['access_status'] == 'active'
    assert [alias['access_status'] for alias in resources['profiles'][0]['aliases']] == ['blocked','active']


def test_subscription_raw_only_cannot_reveal_yaml_or_gui_import(environment):
    env = environment
    env.config['subscription']['formats'] = ['raw']
    (env.directory/'config.json').write_bytes(canonical(env.config))
    result = env.service.handle('subscription.reveal',{'format':'links'})
    assert set(result['links']) == {'raw'} and 'imports' not in result
    with pytest.raises(ManagementError) as error:
        env.service.handle('subscription.reveal',{'format':'yaml'})
    assert error.value.code == 'VALIDATION'


def test_operation_exception_cannot_enter_jobs_or_overview(environment,monkeypatch):
    env = environment
    def failed(*args,**kwargs):
        raise RuntimeError('runtime output contains '+env.config['subscription']['token'])
    monkeypatch.setattr(deployment,'prepare',failed)
    job = finish_job(env.service,'plan.create',{'kind':'website.update','base_revision':0,'values':{'site_title':'Changed'}})
    assert job['status'] == 'failed' and job['error']['code'] == 'OPERATION_FAILED'
    assert_private_fields_absent(job,env.config)
    assert_private_fields_absent(env.service.handle('overview',{}),env.config)


def test_actual_jobs_do_not_persist_certificate_inputs(environment,monkeypatch):
    env = environment
    secret = 'PRIVATE-CERTIFICATE-INPUT-NOT-A-JOB-RESULT'
    monkeypatch.setattr(certificates,'import_pair',lambda *a,**kw:{
        'certificate_path':env.config['server']['certificate_path'],
        'key_path':env.config['server']['key_path'],'sha256':'d'*64,'not_after':'2030-01-01',
    })
    job = finish_job(env.service,'plan.create',{'kind':'certificate.import','base_revision':0,
                      'values':{'certificate_pem':'PUBLIC-TEST-FIXTURE','private_key_pem':secret}})
    assert job['status'] == 'succeeded'
    assert secret not in json.dumps(job)
    assert secret.encode() not in env.service.jobs.path.read_bytes()
    assert list((env.root/'certificates').iterdir()) == []


def test_jobs_process_interruption_is_durable_and_visible_after_restart(tmp_path):
    root = tmp_path/'interrupted-jobs'
    script = '''
import json, os, sys, threading
from pathlib import Path
from proxyhub.jobs import Jobs
event = threading.Event()
jobs = Jobs(Path(sys.argv[1]))
def interrupted():
    event.wait()
    os._exit(0)
result = jobs.submit("test.interrupted", interrupted)
print(json.dumps(result), flush=True)
event.set()
event.wait()
'''
    environment = dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    completed = subprocess.run([sys.executable,'-c',script,str(root)],capture_output=True,text=True,
                               env=environment,timeout=30,check=True)
    identity = json.loads(completed.stdout)['job_id']
    with sqlite3.connect(root/'panel-jobs.sqlite3') as db:
        assert db.execute('SELECT status FROM jobs WHERE id=?',(identity,)).fetchone()[0] == 'running'
    jobs = Jobs(root)
    try:
        result = jobs.get(identity)
        assert result['status'] == 'interrupted' and result['result'] is None
        assert result['error']['code'] == 'INTERRUPTED' and result['finished_at'] is not None
    finally:
        jobs.close()


@pytest.fixture
def ssh_projection(monkeypatch):
    context = {'addr':'203.0.113.1','host':'client.example.test','laddr':'203.0.113.2','lport':'22'}
    source = {'text':'pubkeyauthentication yes\nauthorizedkeysfile .ssh/authorized_keys\nauthorizedkeyscommand none\ndisableforwarding no\nallowtcpforwarding yes\n'}
    monkeypatch.setattr(management_read,'accounts',lambda:['root'])
    monkeypatch.setattr(ssh,'_account',lambda *a:{'name':'root','home':'/root','uid':0,'gid':0})
    original = ssh._effective
    monkeypatch.setattr(ssh,'_effective',lambda context,runner,path:original(context,runner,os.path.abspath('sshd-test-fixture')))
    monkeypatch.setattr(management_read,'run',lambda *a,**kw:source['text'])
    return context,source


def test_ssh_policy_uses_same_output_without_changing_host_plan_projection(ssh_projection):
    context,source = ssh_projection
    result = management_read.ssh_details('root',context)
    assert result['tunnel_policy_allowed'] is True
    assert result['tunnel_verification'] == 'unverified'
    expected = hashlib.sha256(canonical(['root',ssh._context('root',context),
                    hashlib.sha256(source['text'].encode()).hexdigest(),'/root/.ssh/authorized_keys'])).hexdigest()
    assert result['paths'] == [{'id':expected,'label':'/root/.ssh/authorized_keys'}]
    source['text'] = source['text'].replace('disableforwarding no','disableforwarding yes')
    changed = management_read.ssh_details('root',context)
    assert changed['tunnel_policy_allowed'] is False and changed['paths'][0]['id'] != expected


def test_ssh_unknown_forwarding_policy_is_not_reported_as_prohibited(ssh_projection):
    context,source = ssh_projection
    source['text'] = 'pubkeyauthentication yes\nauthorizedkeysfile .ssh/authorized_keys\n'
    assert management_read.ssh_details('root',context)['tunnel_policy_allowed'] is None


def test_ssh_path_id_rechecked_against_live_effective_configuration(environment,ssh_projection):
    env = environment
    context,source = ssh_projection
    path_id = management_read.ssh_details('root',context)['paths'][0]['id']
    source['text'] = source['text'].replace('.ssh/authorized_keys','.ssh/another_keys')
    for invalid in (path_id,'/etc/shadow','../../root/.ssh/authorized_keys'):
        with pytest.raises(ManagementError) as error:
            env.service.create_plan('ssh.append',0,{'account':'root','context':context,'path_id':invalid,'public_key':'fixture'})
        assert error.value.code == 'STALE_REVISION'


def test_ssh_append_result_is_unverified_and_omits_private_plan(environment,monkeypatch):
    env = environment
    monkeypatch.setattr(management,'ssh_details',lambda *a:{'paths':[{'id':'safe-id','label':'/root/.ssh/custom'}]})
    seen = {}
    def plan(account,key,context,selected_path):
        seen.update(account=account,key=key,path=selected_path)
        return {'fingerprint':'SHA256:public-fixture','status':'noop','key':{'line':key}}
    monkeypatch.setattr(ssh,'plan_append',plan)
    preview = env.service.create_plan('ssh.append',0,{'account':'root','context':{},'path_id':'safe-id','public_key':'public fixture\r\n'})
    monkeypatch.setattr(ssh,'apply_append',lambda payload:{'status':'noop','verification':'unverified','fingerprint':'SHA256:public-fixture','backup':'private-backup'})
    result = env.service.apply_plan(preview['plan_id'])
    assert seen == {'account':'root','key':'public fixture','path':'/root/.ssh/custom'}
    assert result == {'status':'noop','verification':'unverified','fingerprint':'SHA256:public-fixture'}
    assert 'public fixture' not in json.dumps(preview)


@pytest.mark.parametrize('managed',[True,False])
def test_certificate_import_keeps_private_pem_out_of_preview_and_reports_ownership(environment,monkeypatch,managed):
    env = environment
    pem_secret = 'PRIVATE-PEM-TEST-CANARY'
    def imported(certificate,key,domains):
        assert certificate.read_text() == 'PUBLIC-PEM-TEST-FIXTURE'
        assert key.read_text() == pem_secret
        return {'certificate_path':env.config['server']['certificate_path'],'key_path':env.config['server']['key_path'],
                'sha256':'d'*64,'not_after':'2030-01-01','private_key':pem_secret}
    monkeypatch.setattr(certificates,'import_pair',imported)
    monkeypatch.setattr(certificates,'acme_source',lambda binding:{'lineage':'proxyhub'} if managed else None)
    result = env.service.create_plan('certificate.import',0,{'certificate_pem':'PUBLIC-PEM-TEST-FIXTURE','private_key_pem':pem_secret})
    assert pem_secret not in json.dumps(result)
    assert ('保留自动续期' if managed else '不接管自动续期') in result['warnings'][0]
    assert list((env.root/'certificates').iterdir()) == []


def fake_activation(store,identity):
    store.record(identity,'activating',{'previous':None,'candidate':'fixture'})
    store.commit(identity)
    return {'status':'applied'}


def test_renewal_changed_certificate_commits_parent_and_child_once(environment,monkeypatch):
    env = environment
    preview = env.service.create_plan('certificate.renew',0,{})
    monkeypatch.setattr(certificates,'issue',lambda *a,**kw:{'certificate_path':'new-cert','key_path':'new-key'})
    monkeypatch.setattr(deployment,'apply',fake_activation)
    assert env.service.apply_plan(preview['plan_id'])['status'] == 'applied'
    with env.service.state() as store:
        child = store.renewal_child(preview['plan_id'])
        assert child is not None
        assert store.inspect(preview['plan_id'])['status'] == 'applied'
        assert store.inspect(child)['status'] == 'applied'
        assert store.revision == 1 and store.pending() == []
        with pytest.raises(OperationError):
            store.get(preview['plan_id'])


def test_linked_renewal_retries_existing_candidate_without_reissuing(environment,monkeypatch):
    env = environment
    preview = env.service.create_plan('certificate.renew',0,{})
    with env.service.state() as store:
        child = store.plan('deployment',{})
        store.link_renewal(preview['plan_id'],child)
    monkeypatch.setattr(certificates,'issue',lambda *a,**kw:pytest.fail('Must not issue twice'))
    monkeypatch.setattr(deployment,'apply',fake_activation)
    assert env.service.apply_plan(preview['plan_id'])['status'] == 'applied'


def test_renewal_without_certificate_change_closes_original_plan(environment,monkeypatch):
    env = environment
    preview = env.service.create_plan('certificate.renew',0,{})
    monkeypatch.setattr(certificates,'issue',lambda *a,**kw:{'certificate_path':env.config['server']['certificate_path']})
    assert env.service.apply_plan(preview['plan_id'])['status'] == 'unchanged'
    with env.service.state() as store:
        assert store.inspect(preview['plan_id'])['status'] == 'applied'
        assert store.renewal_child(preview['plan_id']) is None
    assert env.prepared == []


def test_external_certificate_cannot_be_taken_over_for_renewal(environment,monkeypatch):
    env = environment
    monkeypatch.setattr(certificates,'managed_acme',lambda server:False)
    with pytest.raises(ManagementError) as error:
        env.service.create_plan('certificate.renew',0,{})
    assert error.value.code == 'UNSUPPORTED'


def test_renewal_rejects_concurrent_configuration_change(environment,monkeypatch):
    env = environment
    preview = env.service.create_plan('certificate.renew',0,{})
    def changed(*args,**kwargs):
        with env.service.state() as store:
            store.commit(store.plan('ssh-append',{}))
        return {'certificate_path':'new-cert','key_path':'new-key'}
    monkeypatch.setattr(certificates,'issue',changed)
    with pytest.raises(ManagementError) as error:
        env.service.apply_plan(preview['plan_id'])
    assert error.value.code == 'STALE_REVISION' and env.prepared == []


def test_child_rollback_atomically_marks_renewal_failed_without_revision_increment(environment):
    env = environment
    with env.service.state() as store:
        parent = store.plan('panel-renew',{})
        child = store.plan('deployment',{})
        store.link_renewal(parent,child)
        store.record(child,'activating',{'previous':None,'candidate':'fixture'})
        store.abort(child,{'recovered':True})
        assert store.inspect(parent)['status'] == 'failed' and store.inspect(child)['status'] == 'failed'
        assert store.revision == 0 and store.pending() == []


def test_renewal_parent_and_child_commit_are_one_sqlite_transaction(environment):
    env = environment
    with env.service.state() as store:
        parent = store.plan('panel-renew',{})
        child = store.plan('deployment',{})
        store.link_renewal(parent,child)
        store.record(child,'activating',{'previous':None,'candidate':'fixture'})
        store.db.execute("CREATE TRIGGER fail_parent BEFORE UPDATE ON plans WHEN OLD.kind='panel-renew' BEGIN SELECT RAISE(ABORT,'injected parent failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.commit(child)
        assert store.inspect(parent)['status'] == 'prepared' and store.inspect(child)['status'] == 'prepared'
        assert store.revision == 0 and len(store.pending()) == 1
        store.db.execute('DROP TRIGGER fail_parent')
        store.commit(child)
        assert store.revision == 1 and store.pending() == []


def test_renewal_linkage_rejects_unrelated_or_stale_plans(environment):
    env = environment
    with env.service.state() as store:
        parent = store.plan('ssh-append',{})
        child = store.plan('deployment',{})
        with pytest.raises(OperationError,match='requires a renewal'):
            store.link_renewal(parent,child)
        renewal = store.plan('panel-renew',{})
        store.commit(parent)
        with pytest.raises(OperationError,match='stale'):
            store.link_renewal(renewal,child)
