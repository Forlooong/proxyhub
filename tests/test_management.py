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
    assert arguments == {'expected_revision':0,'publish_only':True}
    assert candidate['server'] == env.config['server']
    assert candidate['subscription']['token'] == env.config['subscription']['token']
    assert candidate['overrides'] == values['overrides']
    assert_private_fields_absent(job,env.config)


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
    monkeypatch.setattr(certificates,'managed_acme',lambda server:managed)
    result = env.service.create_plan('certificate.import',0,{'certificate_pem':'PUBLIC-PEM-TEST-FIXTURE','private_key_pem':pem_secret})
    assert pem_secret not in json.dumps(result)
    assert ('保留已验证' if managed else '不接管自动续期') in result['warnings'][0]
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
