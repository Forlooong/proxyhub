"""Opt-in shared-VPS stage 3a acceptance; keep secrets in the private work directory.

Run as root with no concurrent writers and an external resource sampler/REALITY
retention check. Only subscription publication and explicit publisher/manager
fault windows are exercised. This does not perform protocol or GUI acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def private_json(path, value):
    from proxyhub.core import atomic_write, canonical
    atomic_write(path, canonical(value), 0o600)


def record(work, run_id, stage, event, **fields):
    value = {'utc':utc(),'monotonic_ns':time.monotonic_ns(),'run_id':run_id,
             'stage':stage,'event':event,**fields}
    with (work/'events.jsonl').open('a',encoding='utf-8') as stream:
        os.chmod(work/'events.jsonl',0o600)
        stream.write(json.dumps(value,ensure_ascii=False)+'\n')
        stream.flush()


def crash_child(args):
    from proxyhub import deployment as d
    from proxyhub.state import StateStore
    state=json.loads((args.work/'state.json').read_text(encoding='utf-8'))
    def exit_now():
        record(args.work,state['run_id'],args.crash_point,'injected_process_exit',
               transaction=args.child_plan,exit_code=86)
        os._exit(86)
    store=StateStore(d.ROOT/'plans')
    if args.crash_point=='before-gate':
        original_record=store.record
        def activating(identity,stage,details):
            original_record(identity,stage,details)
            if stage=='activating':
                pending=store.pending()
                assert len(pending)==1 and pending[0]['plan_id']==args.child_plan
                assert (d.ROOT/'publish').stat().st_mode & 0o777==0o750
                record(args.work,state['run_id'],args.crash_point,'durable_safety_intent_before_read_gate',
                       transaction=args.child_plan,pending=1,gate_mode='0750')
                exit_now()
        store.record=activating
    elif args.crash_point=='safety-gate':
        original_gate=d._publication_gate
        def gate(*values,**options):
            original_gate(*values,**options)
            if options.get('closed'):
                pending=store.pending()
                assert len(pending)==1 and pending[0]['plan_id']==args.child_plan
                record(args.work,state['run_id'],args.crash_point,'read_gate_closed_after_durable_intent',
                       transaction=args.child_plan,pending=1,gate_mode='0700')
                exit_now()
        d._publication_gate=gate
    elif args.crash_point=='safety-intent':
        def revoke(*values,**options):
            assert len(store.pending())==1
            record(args.work,state['run_id'],args.crash_point,'intent_precedes_first_policy_write',
                   transaction=args.child_plan)
            exit_now()
        d._revoke_publication_changes=revoke
    elif args.crash_point=='safety-stop':
        original_run=d.run
        def fail_advance(*values,**options):
            record(args.work,state['run_id'],args.crash_point,'injected_safety_ledger_write_failure',
                   transaction=args.child_plan,error_class='OSError')
            try:
                raise OSError('Injected publication safety write failure before completed-stop exit')
            except OSError:
                with (args.work/('child-'+args.child_plan+'-write-failure.log')).open('a',encoding='utf-8') as stream:
                    os.chmod(stream.name,0o600)
                    stream.write(traceback.format_exc())
                raise
        def stop(command,**options):
            result=original_run(command,**options)
            if command==['systemctl','stop','proxyhub-publisher.service']:
                active=original_run(['systemctl','show','proxyhub-publisher.service',
                                     '--property=ActiveState','--value']).strip()
                assert active=='inactive' and len(store.pending())==1
                record(args.work,state['run_id'],args.crash_point,'publisher_stop_completed_before_exit',
                       transaction=args.child_plan,active_state=active)
                exit_now()
            return result
        d.advance_security,d.run=fail_advance,stop
    elif args.crash_point=='after-revocation':
        # Stop at _link entry after both the safety intent journal and durable
        # revocation, before switching the current revision.
        def link(*values,**options):
            assert len(store.pending())==1
            exit_now()
        d._link=link
    else:
        original=d._publish
        def publish(*values,**options):
            original(*values,**options)
            exit_now()
        d._publish=publish
    try:
        d.apply(store,args.child_plan)
    finally:
        store.db.close()
    raise RuntimeError('The controlled child did not reach its exit boundary')


class Acceptance:
    def __init__(self,args):
        from proxyhub import deployment, subscriptions
        from proxyhub.core import canonical
        from proxyhub.publication import iter_routes,load_security,route_allowed,token_digest
        from proxyhub.state import StateStore
        self.d,self.s,self.canonical=deployment,subscriptions,canonical
        self.iter_routes,self.load_security=iter_routes,load_security
        self.route_allowed,self.token_digest=route_allowed,token_digest
        self.work,self.report_path=args.work,args.report
        self.state_path=self.work/'state.json'
        if self.state_path.exists():
            if not args.resume:
                raise RuntimeError('Existing evidence requires explicit --resume')
            self.state=json.loads(self.state_path.read_text(encoding='utf-8'))
        else:
            self.state={'run_id':secrets.token_hex(8),'completed':[],'profiles':{},'checks':{}}
            self.save()
        self.stage='initializing'
        self.transaction=None
        self.store=StateStore(self.d.ROOT/'plans')
        self.original_health=self.d._publication_health
        self.d._publication_health=self.health

    def save(self):
        private_json(self.state_path,self.state)

    def event(self,event,**fields):
        record(self.work,self.state['run_id'],self.stage,event,transaction=self.transaction,**fields)

    def mark(self,stage):
        self.stage=stage
        private_json(self.work/'stage.json',{'run_id':self.state['run_id'],
                     'transaction':self.transaction,'stage':stage})
        self.event('stage_started')

    def step(self,name,action):
        if name in self.state['completed']:
            return
        self.mark(name)
        action()
        self.state['completed'].append(name)
        self.state['checks'][name]='passed'
        self.save()
        self.event('stage_passed')

    def active(self):
        directory=self.d._current_revision()
        return directory,json.loads((directory/'config.json').read_text(encoding='utf-8'))

    def health(self,directory,*,readiness_only=False):
        started=time.monotonic()
        purpose='publisher-readiness' if readiness_only else 'all-profile-confirmation'
        self.event('publication_ack_started',revision=directory.name,purpose=purpose)
        try:
            self.original_health(directory,readiness_only=readiness_only)
        except Exception as error:
            self.event('publication_ack_failed',revision=directory.name,purpose=purpose,
                       error_class=type(error).__name__,elapsed_ms=round((time.monotonic()-started)*1000,3))
            with (self.work/'health-failures.log').open('a',encoding='utf-8') as stream:
                os.chmod(self.work/'health-failures.log',0o600)
                stream.write(utc()+' '+self.stage+' '+str(self.transaction)+'\n'+traceback.format_exc()+'\n')
            raise
        self.event('publication_ack_passed',revision=directory.name,purpose=purpose,
                   elapsed_ms=round((time.monotonic()-started)*1000,3))

    def prepare(self,config,operation=None):
        prepared=self.d.prepare(self.store,config,publish_only=True,
                               subscription_operation=operation,expected_revision=self.store.revision)
        self.transaction=prepared['plan_id']
        self.state['last_plan']=self.transaction
        self.save()
        self.mark(self.stage)
        return prepared

    def apply_config(self,config,operation=None):
        prepared=self.prepare(config,operation)
        result=self.d.apply(self.store,prepared['plan_id'])
        self.event('plan_applied',revision=result['revision'])
        return result

    def change(self,kind,values):
        _,before=self.active()
        after=self.s.change(before,kind,values)
        identity=(next(item['id'] for item in self.s.normalize(after)
                       if item['id'] not in {entry['id'] for entry in self.s.normalize(before)})
                  if kind in ('subscription.create','subscription.copy') else values['subscription_id'])
        self.apply_config(after,{'kind':kind,'subscription_id':identity})
        return identity

    def profile(self,label,config=None):
        return self.s.get_profile(config or self.active()[1],self.state['profiles'][label])

    def request(self,route,method='GET'):
        client=http.client.HTTPConnection('127.0.0.1',18890,timeout=3)
        try:
            client.request(method,route['path'],headers={'Host':route['host'],'Connection':'close'})
            response=client.getresponse()
            return response.status,dict(response.getheaders()),response.read(1024*1024+1)
        finally:
            client.close()

    def bundle(self,directory=None):
        return json.loads(((directory or self.active()[0])/'bundle.json').read_text(encoding='utf-8'))

    def routes(self,bundle,identity=None):
        return [(profile,route) for profile in bundle['profiles'] if identity is None or profile['id']==identity
                for route in self.iter_routes(profile)]

    def assert_denied(self,bundle,identity):
        count=0
        for _,route in self.routes(bundle,identity):
            for method in ('GET','HEAD'):
                status,headers,body=self.request(route,method)
                assert status==404 and not body, 'Revoked route accepted a new request'
                assert headers.get('X-ProxyHub-Security-Generation')==str(self.security()['generation'])
                count+=1
        assert count, 'No historical routes were checked'
        self.event('historical_routes_denied',requests=count)

    def security(self):
        return self.load_security(self.d.ROOT/'publish/security.json')

    def process_ids(self):
        directory,config=self.active()
        units=[*self.d._reality_units(config).values(),*self.d._runtime_units(config,directory),
               'proxyhub-nginx.service']
        result={unit:int(self.d.run(['systemctl','show',unit,'--property=MainPID','--value']).strip()) for unit in units}
        assert all(result.values()), 'A retained core or Nginx service is not running'
        return result

    def websites(self):
        from proxyhub.websites import normalize_web
        _,config=self.active()
        result={}
        for site in normalize_web(config)['sites']:
            if not site['enabled']:
                continue
            host=site['domains'][0]
            with socket.create_connection(('127.0.0.1',site['tls_port']),timeout=5) as raw:
                with ssl.create_default_context().wrap_socket(raw,server_hostname=host) as stream:
                    stream.sendall(f'GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
                    response=http.client.HTTPResponse(stream)
                    response.begin()
                    assert response.status==200, 'Preserved website is unavailable'
                    result[site['id']]=sha(response.read(2*1024*1024))
                    response.close()
        return result

    def preserved(self):
        directory,config=self.active()
        keep={key:value for key,value in config.items() if key!='subscriptions'}
        static={path.relative_to(directory).as_posix():sha(path.read_bytes())
                for parent in (directory/'site',directory/'sites') if parent.exists()
                for path in parent.rglob('*') if path.is_file()}
        default=next(item for item in self.bundle()['profiles'] if item['id']=='default')
        return {'config_sha256':sha(self.canonical(keep)),
                'client_yaml_sha256':sha((directory/'client.yaml').read_bytes()),
                'default_routes_sha256':sha(self.canonical(list(self.iter_routes(default)))),
                'static_files':static,'websites':self.websites(),'process_ids':self.process_ids()}

    def initialize(self):
        assert not self.store.pending(), 'Recover the existing activation before acceptance'
        directory,config=self.active()
        assert (directory/'shared.json').is_file(), 'Only the existing shared layout is permitted'
        self.d._ensure_publication_security(self.store)
        self.d._wait_publication(directory)
        baseline=self.work/'baseline.json'
        if not baseline.exists():
            private_json(baseline,{'utc':utc(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                         'preserved':self.preserved(),'original_config':config,'revision':directory.name})
        enabled=[item for item in self.s.node_resources(config) if item['enabled'] and item['id']!='local-reality']
        enabled.sort(key=lambda item:({'ss':0,'vmess':1}.get(item['protocol'],2),item['id']))
        assert len(enabled)>=2, 'Two existing enabled node identities are required'
        self.state.setdefault('selected_nodes',[enabled[0]['id'],enabled[1]['id']])
        self.save()

    def initialize_blockers(self):
        expected={'A':'2f584df037fae599113b9d5049cfb335',
                  'B':'b82aaae3ba002bb3638f0e72738e1fbf'}
        assert self.state['profiles'] in ({},expected), 'Blocker acceptance must reuse the existing profiles'
        self.state['profiles']=expected
        self.initialize()
        assert {item['id'] for item in self.s.normalize(self.active()[1])}=={'default',*expected.values()}
        self.state['blocker_only']=True
        self.save()

    def boundary_command(self,label,command):
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60)
        log=self.work/(self.stage+'-service.log')
        with log.open('ab') as stream:
            log.chmod(0o600)
            stream.write((utc()+' '+label+' '+json.dumps(command)+'\n').encode())
            stream.write(result.stdout+b'\n')
        self.event('real_boundary_command',label=label,exit_code=result.returncode)
        return result

    def assert_closed(self,bundle):
        refused=0
        for _,route in self.routes(bundle):
            for method in ('GET','HEAD'):
                try:
                    status,_,_=self.request(route,method)
                except (OSError,http.client.HTTPException) as error:
                    self.event('publication_request_refused',method=method,error_class=type(error).__name__)
                else:
                    assert status!=200, 'An unconfirmed safety operation reopened a subscription'
                    self.event('publication_request_refused',method=method,status=status)
                refused+=1
        assert refused
        self.event('all_subscription_publication_temporarily_closed',requests=refused)

    def cleanup_blocker_aliases(self):
        identity=self.state['profiles']['A']
        for alias in self.state.get('blocker_aliases',[]):
            if any(item['id']==alias for item in self.profile('A')['aliases']):
                self.change('subscription.alias.retire',{'subscription_id':identity,'alias_id':alias})
                self.event('temporary_alias_cleaned',alias_id=alias)

    def startup_gate_boundary(self,*,gate_closed=False):
        """The real unit/uid boundary, including a precheck already in flight."""
        identity=self.state['profiles']['A']
        previous,config=self.active()
        historical=self.bundle()
        changed=self.s.change(config,'subscription.token.rotate',{'subscription_id':identity})
        plan=self.prepare(changed,{'kind':'subscription.token.rotate','subscription_id':identity})
        drop=Path('/etc/systemd/system/proxyhub-publisher.service.d/phase3a-blocker.conf')
        ready,release=self.work/'startup-ready',self.work/'startup-release'
        starter=None
        drop_created=False
        try:
            if not gate_closed:
                assert not drop.exists() and not ready.exists() and not release.exists()
                self.boundary_command('stop-before-precheck',['systemctl','stop','proxyhub-publisher.service'])
                drop.parent.mkdir(exist_ok=True)
                drop.write_text('[Service]\nTimeoutStartSec=40\nExecStartPre=+/usr/bin/python3 '+
                    str(Path(__file__).resolve())+' --work '+str(self.work)+' --startup-delay\n')
                drop_created=True
                drop.chmod(0o644)
                assert self.boundary_command('load-controlled-delay',['systemctl','daemon-reload']).returncode==0
                output=(self.work/'prechecked-start.log').open('ab')
                starter=subprocess.Popen(['systemctl','start','proxyhub-publisher.service'],stdout=output,stderr=output)
                output.close()
                deadline=time.monotonic()+15
                while not ready.exists():
                    if starter.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('Standard startup did not reach the post-guard delay')
                    time.sleep(.05)
                self.event('standard_start_precheck_passed_before_safety_intent')
            point='safety-gate' if gate_closed else 'safety-intent'
            log=self.work/(self.stage+'-child.log')
            with log.open('ab') as stream:
                log.chmod(0o600)
                child=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--work',str(self.work),
                    '--child-plan',plan['plan_id'],'--crash-point',point],stdout=stream,stderr=stream,timeout=30)
            assert child.returncode==86
            from proxyhub.state import StateStore
            self.store.db.close()
            self.store=StateStore(self.d.ROOT/'plans')
            pending=self.store.pending()
            assert len(pending)==1 and pending[0]['plan_id']==plan['plan_id']
            assert (self.d.ROOT/'publish').stat().st_mode & 0o777==0o700
            self.event('startup_gate_child_exited',exit_code=86,pending=len(self.store.pending()),
                       gate_mode='0700',injection_point=point)
            if starter:
                release.touch(mode=0o600)
                status=starter.wait(timeout=15)
                # Type=simple reports the spawned Python process as started
                # before it opens its bundle. Inspect its actual exit and I/O.
                self.event('prechecked_standard_start_command_completed',exit_code=status)
                deadline=time.monotonic()+5
                while True:
                    state=self.d.run(['systemctl','show','proxyhub-publisher.service',
                                     '--property=MainPID,ExecMainStatus,ExecMainCode'])
                    if 'MainPID=0\n' in state and 'ExecMainStatus=2\n' in state:
                        break
                    if time.monotonic()>deadline:
                        raise AssertionError('Prechecked publisher did not fail its unprivileged bundle open')
                    time.sleep(.05)
                self.event('prechecked_unprivileged_publisher_startup_rejected',process_exit_code=2)
            else:
                assert self.boundary_command('owned-closed-gate-start-refused',
                    ['systemctl','restart','proxyhub-publisher.service']).returncode!=0
            self.boundary_command('post-gate-service-state',['systemctl','show','proxyhub-publisher.service',
                '--property=User,Group,ActiveState,SubState,ExecStartPre,ExecMainCode,ExecMainStatus'])
            self.boundary_command('post-gate-journal',['journalctl','-u','proxyhub-publisher.service',
                '--no-pager','-n','45','-o','short-iso-precise'])
            self.assert_closed(historical)
        finally:
            try:
                if starter and starter.poll() is None:
                    release.touch(mode=0o600)
                    try:
                        starter.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        self.boundary_command('stop-timed-out-controlled-start',
                                              ['systemctl','stop','proxyhub-publisher.service'])
                        raise
            finally:
                if drop_created:
                    drop.unlink(missing_ok=True)
                    if not any(drop.parent.iterdir()):
                        drop.parent.rmdir()
                    assert self.boundary_command('remove-controlled-delay',['systemctl','daemon-reload']).returncode==0
                ready.unlink(missing_ok=True)
                release.unlink(missing_ok=True)
        assert self.d.recover(self.store)['status']=='recovered'
        self.assert_denied(historical,identity)
        self.change('subscription.token.rotate',{'subscription_id':identity})
        assert not self.store.pending()
        self.d._wait_publication(self.active()[0])
        self.assert_denied(historical,identity)

    def blocker_case(self,kind,point):
        from proxyhub.core import OperationError
        identity=self.state['profiles']['A']
        if self.token_digest(self.profile('A')['token']) in self.security()['revoked_tokens']:
            self.change('subscription.token.rotate',{'subscription_id':identity})
        self.cleanup_blocker_aliases()
        selected=None
        if kind=='subscription.alias.retire':
            aliases={item['id'] for item in self.profile('A')['aliases']}
            self.change('subscription.alias.add',{'subscription_id':identity,
                'host':self.profile('A')['host'],
                'path_prefix':'/phase3a-blocker-'+secrets.token_hex(6)})
            selected=next(item for item in self.profile('A')['aliases'] if item['id'] not in aliases)
            self.state.setdefault('blocker_aliases',[]).append(selected['id'])
            self.save()
        old_directory,old_config=self.active()
        historical=self.bundle(old_directory)
        values={'subscription_id':identity}
        if selected:
            values['alias_id']=selected['id']
        changed=self.s.change(old_config,kind,values)
        candidate_token=self.s.get_profile(changed,identity)['token']
        plan=self.prepare(changed,{'kind':kind,'subscription_id':identity})
        log=self.work/(self.stage+'-child.log')
        with log.open('ab') as stream:
            log.chmod(0o600)
            child=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--work',str(self.work),
                '--child-plan',plan['plan_id'],'--crash-point',point],
                stdout=stream,stderr=subprocess.STDOUT,timeout=120)
        self.event('controlled_child_completed',injection_point=point,exit_code=child.returncode)
        assert child.returncode==86, 'Controlled child did not reach the required exit boundary'
        from proxyhub.state import StateStore
        self.store.db.close()
        self.store=StateStore(self.d.ROOT/'plans')
        self.event('state_reopened_from_disk_after_child_exit')
        pending=self.store.pending()
        assert len(pending)==1 and pending[0]['plan_id']==plan['plan_id']
        assert pending[0]['details'].get('publication_safety_failed')
        private_json(self.work/(self.stage+'-pending.json'),
                     {'pending':pending,'plan':self.store.inspect(plan['plan_id'])})
        mode=(self.d.ROOT/'publish').stat().st_mode & 0o777
        assert mode==(0o750 if point=='before-gate' else 0o700)
        self.event('durable_intent_owns_read_gate',gate_mode=f'{mode:04o}',pending=1,
                   injection_point=point)
        if point=='before-gate':
            # The journal binds recovery, but the existing publisher has not
            # entered its refusal window until the read gate actually closes.
            for profile,route in self.routes(historical):
                for method in ('GET','HEAD'):
                    try:
                        status,headers,body=self.request(route,method)
                    except (OSError,http.client.HTTPException) as error:
                        self.event('pre_gate_existing_listener_observation',profile=profile['id'],
                                   method=method,error_class=type(error).__name__)
                    else:
                        self.event('pre_gate_existing_listener_observation',profile=profile['id'],
                                   method=method,status=status,body_bytes=len(body),
                                   generation=headers.get('X-ProxyHub-Generation'))
        if selected:
            from proxyhub.publication import alias_digest
            assert alias_digest(selected['host'],selected['path_prefix']) not in self.security()['retired_aliases']
        else:
            assert self.token_digest(self.profile('A')['token']) not in self.security()['revoked_tokens']
        checked=self.boundary_command('root-read-only-start-check',
                                     ['/usr/local/bin/proxyhub','deployment','check-publication'])
        assert checked.returncode!=0, 'Standard CLI bypassed the unfinished revocation'
        assert b'(OperationError)' in checked.stdout, 'Start check failed outside its intended safety guard'
        restarted=self.boundary_command('standard-service-restart',
                                       ['systemctl','restart','proxyhub-publisher.service'])
        assert restarted.returncode!=0, 'Standard publisher restart bypassed the unfinished revocation'
        self.boundary_command('failed-start-state',['systemctl','show','proxyhub-publisher.service',
            '--property=ActiveState,SubState,Result,ExecStartPre,ExecMainCode,ExecMainStatus'])
        self.boundary_command('start-guard-journal',['journalctl','-u','proxyhub-publisher.service',
                                                   '--no-pager','-n','50','-o','short-iso-precise'])
        self.assert_closed(historical)
        original_advance,original_run=self.d.advance_security,self.d.run
        original_health=self.d._publication_health
        original_connection=http.client.HTTPConnection
        actions,acks=[],[]
        def traced_run(command,**options):
            actions.append(tuple(command))
            if command[:1]==['systemctl']:
                self.event('publisher_recovery_service_action',action=command[1],unit=command[2])
            return original_run(command,**options)
        def traced_health(directory,*,readiness_only=False):
            acks.append((directory.name,readiness_only))
            return original_health(directory,readiness_only=readiness_only)
        def fail_write(*values,**options):
            self.event('injected_persistent_safety_write_failure',error_class='OSError')
            raise OSError('Injected persistent publication safety write failure')
        self.d.run,self.d._publication_health=traced_run,traced_health
        try:
            self.d.advance_security=fail_write
            try:
                self.d.recover(self.store)
            except Exception as error:
                self.event('persistent_failure_preserved_pending',error_class=type(error).__name__)
                with (self.work/(self.stage+'-persistent-failure.log')).open('a',encoding='utf-8') as stream:
                    os.chmod(stream.name,0o600)
                    stream.write(traceback.format_exc())
            else:
                raise AssertionError('Persistent safety failure was accepted')
            assert len(self.store.pending())==1
            assert original_run(['systemctl','show','proxyhub-publisher.service',
                                 '--property=ActiveState','--value']).strip()=='inactive'
            self.assert_closed(historical)
            self.d.advance_security=original_advance
            last_profile,last_route=self.routes(historical)[-1]
            target=(last_route['host'],last_route['path'],'HEAD')
            harness=self
            injected=[]
            class LastProfileFailure(original_connection):
                def request(connection,method,path,*values,**options):
                    connection.probe=(options.get('headers',{}).get('Host'),path,method)
                    return super().request(method,path,*values,**options)
                def getresponse(connection):
                    response=super().getresponse()
                    if connection.probe==target:
                        actual=response.status
                        response.status=503
                        injected.append(actual)
                        harness.event('injected_final_profile_ack_failure',profile=last_profile['id'],
                                      method='HEAD',actual_status=actual,injected_status=503)
                    return response
            http.client.HTTPConnection=LastProfileFailure
            try:
                self.d.recover(self.store)
            except OperationError as error:
                self.event('final_profile_failure_preserved_pending',error_class=type(error).__name__)
                with (self.work/(self.stage+'-ack-failure.log')).open('a',encoding='utf-8') as stream:
                    os.chmod(stream.name,0o600)
                    stream.write(traceback.format_exc())
            else:
                raise AssertionError('A failed final profile ACK cleared the transaction')
            finally:
                http.client.HTTPConnection=original_connection
            assert injected and len(self.store.pending())==1
            ledger=self.security()
            assert self.token_digest(candidate_token) not in ledger['revoked_tokens']
            assert self.d.recover(self.store)['status']=='recovered'
            assert self.security()==ledger and not self.store.pending()
            assert self.active()[1]==old_config
            self.d._wait_publication(self.active()[0])
            before=len(actions),len(acks)
            assert self.d.recover(self.store)['status']=='nothing_to_recover'
            assert (len(actions),len(acks))==before, 'Idempotent recover issued commands or probes'
            self.event('recovery_confirmed_all_profiles',profiles=[item['id'] for item in historical['profiles']],
                       methods=['GET','HEAD'],idempotent_service_commands=0,idempotent_probes=0,
                       candidate_token_not_revoked=True,restored_configuration=True)
            if selected:
                for _,route in self.routes(historical,identity):
                    if route['host']==selected['host'] and route['path_prefix']==selected['path_prefix']:
                        for method in ('GET','HEAD'):
                            status,_,body=self.request(route,method)
                            assert status==404 and not body
                self.cleanup_blocker_aliases()
            else:
                self.assert_denied(historical,identity)
                self.change('subscription.token.rotate',{'subscription_id':identity})
                assert self.token_digest(candidate_token) not in self.security()['revoked_tokens']
            self.d._wait_publication(self.active()[0])
        finally:
            self.d.advance_security,self.d.run=original_advance,original_run
            self.d._publication_health=original_health
            http.client.HTTPConnection=original_connection

    def create_profiles(self):
        for label in ('A','B'):
            if label in self.state['profiles']:
                continue
            if label=='A':
                identity=self.change('subscription.create',{
                    'name':'ProxyHub 3a 验收 A','path_prefix':'/phase3a-a',
                    'node_ids':['local-reality',self.state['selected_nodes'][0]],
                    'overrides':{'mixed-port':17891,'log-level':'warning'},'update_interval_hours':6,
                    'userinfo':{'upload':1048576,'download':2097152,'total':1073741824,
                                'expire':int(time.time())+30*86400}})
            else:
                identity=self.change('subscription.copy',{'subscription_id':self.state['profiles']['A'],
                                                         'name':'ProxyHub 3a 验收 B'})
            self.state['profiles'][label]=identity
            self.save()
        identity=self.state['profiles']['B']
        selected=['local-reality',self.state['selected_nodes'][1]]
        if self.profile('B')['node_ids']!=selected:
            self.change('subscription.update',{'subscription_id':identity,'node_ids':selected,
                'path_prefix':'/phase3a-b','overrides':{'mixed-port':17892,'log-level':'info'},
                'userinfo':{'expire':int(time.time())+60*86400},'update_interval_hours':12})
        a,b=self.profile('A'),self.profile('B')
        assert set(a['node_ids'])&set(b['node_ids'])=={'local-reality'}
        assert a['overrides']!=b['overrides'] and a['userinfo']!=b['userinfo']

    def lifecycle(self):
        identity=self.state['profiles']['B']
        before=self.bundle()
        if self.profile('B')['enabled']:
            self.change('subscription.disable',{'subscription_id':identity})
        self.assert_denied(before,identity)
        self.change('subscription.enable',{'subscription_id':identity})
        self.d._wait_publication(self.active()[0])

    def aliases(self):
        identity=self.state['profiles']['A']
        _,config=self.active()
        target=next((host for host in sorted(self.s.allowed_hosts(config))
                     if host!=config['server']['subscription_domain']),None)
        assert target, 'An existing managed website domain is required for host migration'
        self.change('subscription.update',{'subscription_id':identity,'host':target,
                                          'path_prefix':'/phase3a-a-current'})
        self.change('subscription.alias.add',{'subscription_id':identity,
                    'host':config['server']['subscription_domain'],'path_prefix':'/phase3a-a-extra'})
        old_directory,old_config=self.active()
        historical=self.bundle(old_directory)
        # The copied profile intentionally shares the original prefix with a
        # different token. Retire the unique extra alias, not that shared host.
        selected=next(alias for alias in self.profile('A')['aliases'] if alias['path_prefix']=='/phase3a-a-extra')
        self.change('subscription.alias.retire',{'subscription_id':identity,'alias_id':selected['id']})
        good_config=self.active()[1]
        ledger=self.security()
        self.apply_config(old_config)
        assert self.security()==ledger, 'Ordinary restoration changed safety state'
        for _,route in self.routes(historical,identity):
            status,_,_=self.request(route)
            assert status==(404 if route['path_prefix']==selected['path_prefix'] and route['host']==selected['host'] else 200)
        self.apply_config(good_config)

    def concurrent_publication(self):
        before=self.bundle()
        _,config=self.active()
        a=self.profile('A',config)
        info={**a['userinfo'],'download':a['userinfo']['download']+1}
        changed=self.s.change(config,'subscription.update',{'subscription_id':a['id'],'userinfo':info})
        plan=self.prepare(changed,{'kind':'subscription.update','subscription_id':a['id']})
        after=self.bundle(self.d.ROOT/'revisions'/plan['revision'])
        candidates={bundle['generation']:{(route['host'],route['path']):(route['headers'],route['body'].encode())
                    for _,route in self.routes(bundle)} for bundle in (before,after)}
        routes=[route for _,route in self.routes(before)]
        stop,new_seen=threading.Event(),threading.Event()
        seen=[]
        started=[threading.Event() for _ in range(4)]
        def download(index):
            count=0
            deadline=time.monotonic()+15
            while not stop.is_set() and time.monotonic()<deadline:
                route=routes[(count+index)%len(routes)]
                status,headers,body=self.request(route)
                generation=headers.get('X-ProxyHub-Generation')
                expected_headers,expected_body=candidates[generation][(route['host'],route['path'])]
                assert status==200 and body==expected_body
                assert all(headers.get(key)==value for key,value in expected_headers.items())
                seen.append(generation)
                started[index].set()
                if generation==after['generation']:
                    new_seen.set()
                count+=1
            return count
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(download,index) for index in range(4)]
            try:
                assert all(event.wait(5) for event in started), 'Concurrent download did not start'
                self.d.apply(self.store,plan['plan_id'])
                assert new_seen.wait(5), 'Concurrent downloads did not observe the committed snapshot'
            finally:
                stop.set()
            counts=[future.result(timeout=10) for future in futures]
        assert set(seen)==set(candidates), 'Both publication generations must be observed'
        self.state['concurrent_requests']=sum(counts)

    def manager_stopped(self):
        units=['proxyhub-manager.service','proxyhub-panel.service']
        active=[unit for unit in units if self.d.run(['systemctl','show',unit,'--property=ActiveState','--value']).strip()=='active']
        try:
            if active:
                self.d.run(['systemctl','stop',*active])
            self.d._wait_publication(self.active()[0])
            self.event('last_valid_snapshot_available',stopped_services=len(active))
        finally:
            if active:
                self.d.run(['systemctl','start',*active])

    def failed_publication(self):
        _,config=self.active()
        identity=self.state['profiles']['A']
        changed=self.s.change(config,'subscription.update',{'subscription_id':identity,'name':'3a 故障候选'})
        plan=self.prepare(changed,{'kind':'subscription.update','subscription_id':identity})
        directory=self.d.ROOT/'revisions'/plan['revision']
        original=self.d._publish
        injected=False
        def corrupt(candidate):
            nonlocal injected
            original(candidate)
            if candidate==directory:
                with (self.d.ROOT/'publish/bundle.json').open('wb') as stream:
                    stream.write(b'{"injected":"invalid publication"}')
                    stream.flush()
                    os.fsync(stream.fileno())
                injected=True
                self.event('injected_invalid_runtime_bundle')
        self.d._publish=corrupt
        try:
            try:
                self.d.apply(self.store,plan['plan_id'])
            except Exception:
                self.event('expected_publication_failure')
                with (self.work/'publication-failure.log').open('w',encoding='utf-8') as stream:
                    os.chmod(self.work/'publication-failure.log',0o600)
                    stream.write(traceback.format_exc())
            else:
                raise AssertionError('Invalid publication was incorrectly committed')
        finally:
            self.d._publish=original
        assert injected and self.store.inspect(plan['plan_id'])['status']=='failed'
        assert not self.store.pending() and self.active()[1]==config
        self.d._wait_publication(self.active()[0])

    def safety_write_failure(self):
        """A failed durable revocation closes all publication until recovery."""
        identity=self.state['profiles']['A']
        historical=self.bundle()
        _,config=self.active()
        changed=self.s.change(config,'subscription.token.rotate',{'subscription_id':identity})
        plan=self.prepare(changed,{'kind':'subscription.token.rotate','subscription_id':identity})
        original_advance,original_run=self.d.advance_security,self.d.run
        starts=[]
        def fail_advance(*values,**options):
            self.event('injected_safety_ledger_write_failure',error_class='OSError')
            try:
                raise OSError('Injected publication safety write failure')
            except OSError:
                with (self.work/'safety-write-failure.log').open('a',encoding='utf-8') as stream:
                    os.chmod(self.work/'safety-write-failure.log',0o600)
                    stream.write(utc()+' '+self.stage+' '+str(self.transaction)+'\n'+traceback.format_exc()+'\n')
                raise
        def trace_run(command,**options):
            if len(command)>2 and command[0]=='systemctl' and command[2]=='proxyhub-publisher.service':
                self.event('publisher_service_action',action=command[1])
                if command[1]=='start':
                    starts.append(command[1])
            return original_run(command,**options)
        self.d.advance_security,self.d.run=fail_advance,trace_run
        try:
            for action in (lambda:self.d.apply(self.store,plan['plan_id']),lambda:self.d.recover(self.store)):
                try:
                    action()
                except Exception as error:
                    self.event('expected_safety_persistence_failure',error_class=type(error).__name__)
                else:
                    raise AssertionError('Failed safety persistence was incorrectly accepted')
                pending=self.store.pending()
                assert len(pending)==1 and pending[0]['details'].get('publication_safety_failed')
                active=original_run(['systemctl','show','proxyhub-publisher.service','--property=ActiveState','--value']).strip()
                assert active=='inactive' and not starts, 'Publication restarted before durable revocation'
                refused=0
                for _,route in self.routes(historical):
                    for method in ('GET','HEAD'):
                        try:
                            self.request(route,method)
                        except (OSError,http.client.HTTPException):
                            refused+=1
                        else:
                            raise AssertionError('A subscription endpoint remained reachable after failed safety persistence')
                self.event('all_subscription_publication_temporarily_closed',requests=refused,
                           scope='default and both additional profiles, including aliases')
            self.d.advance_security=original_advance
            assert self.d.recover(self.store)['status']=='recovered'
            assert starts==['start'] and not self.store.pending()
            assert self.d.recover(self.store)['status']=='nothing_to_recover'
            self.assert_denied(historical,identity)
            for profile,route in self.routes(historical):
                if profile['id']!=identity:
                    status,headers,body=self.request(route)
                    assert status==200 and body==route['body'].encode()
                    assert all(headers.get(key)==value for key,value in route['headers'].items())
            self.change('subscription.token.rotate',{'subscription_id':identity})
        finally:
            self.d.advance_security,self.d.run=original_advance,original_run

    def persistent_failure(self):
        _,config=self.active()
        identity=self.state['profiles']['A']
        changed=self.s.change(config,'subscription.update',{'subscription_id':identity,'name':'3a 持续故障候选'})
        plan=self.prepare(changed,{'kind':'subscription.update','subscription_id':identity})
        original=self.d.run
        def lost_reload(command,**options):
            if command==['systemctl','reload','proxyhub-publisher.service']:
                self.event('injected_lost_reload_with_publisher_stopped')
                return ''
            return original(command,**options)
        original(['systemctl','stop','proxyhub-publisher.service'])
        self.d.run=lost_reload
        try:
            for action in (lambda:self.d.apply(self.store,plan['plan_id']),lambda:self.d.recover(self.store)):
                try:
                    action()
                except Exception:
                    self.event('expected_unavailable_ack',pending=len(self.store.pending()))
                else:
                    raise AssertionError('Persistent unavailable runtime was accepted')
                assert len(self.store.pending())==1
        finally:
            self.d.run=original
            original(['systemctl','start','proxyhub-publisher.service'])
            # A process-start acknowledgement is not an HTTP-ready signal.
            # Confirm the persisted snapshot before recovery sends SIGHUP.
            self.d._wait_publication(self.active()[0])
        assert self.d.recover(self.store)['status']=='recovered'
        assert self.d.recover(self.store)['status']=='nothing_to_recover'
        assert not self.store.pending() and self.active()[1]==config

    def crash(self,point):
        identity=self.state['profiles']['A']
        old_bundle=self.bundle()
        _,old_config=self.active()
        changed=self.s.change(old_config,'subscription.token.rotate',{'subscription_id':identity})
        new_token=self.s.get_profile(changed,identity)['token']
        plan=self.prepare(changed,{'kind':'subscription.token.rotate','subscription_id':identity})
        log=self.work/('child-'+point+'.log')
        with log.open('ab') as stream:
            log.chmod(0o600)
            child=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--work',str(self.work),
                '--child-plan',plan['plan_id'],'--crash-point',point],stdout=stream,stderr=subprocess.STDOUT,timeout=120)
        assert child.returncode==86, 'Controlled child exit boundary was not observed'
        assert len(self.store.pending())==1
        ledger=self.security()
        assert self.token_digest(new_token) not in ledger['revoked_tokens']
        self.assert_denied(old_bundle,identity)
        assert self.d.recover(self.store)['status']=='recovered'
        assert self.security()==ledger
        assert self.d.recover(self.store)['status']=='nothing_to_recover'
        self.assert_denied(old_bundle,identity)
        self.change('subscription.token.rotate',{'subscription_id':identity})

    def restore_token(self):
        identity=self.state['profiles']['A']
        historical=self.bundle()
        _,old_config=self.active()
        self.change('subscription.token.rotate',{'subscription_id':identity})
        newest=self.profile('A')['token']
        ledger=self.security()
        self.assert_denied(historical,identity)
        self.apply_config(old_config)
        assert self.security()==ledger, 'Restoring old configuration revoked the newer token'
        assert self.token_digest(newest) not in ledger['revoked_tokens']
        self.assert_denied(historical,identity)
        self.change('subscription.token.rotate',{'subscription_id':identity})

    def finish(self):
        baseline=json.loads((self.work/'baseline.json').read_text(encoding='utf-8'))
        assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==baseline['boot_id']
        assert self.preserved()==baseline['preserved'], 'A retained configuration, entry, website or core PID changed'
        assert not self.store.pending()
        self.d._wait_publication(self.active()[0])
        assert all(self.profile(label)['enabled'] for label in ('A','B'))
        self.state['final']={'utc':utc(),'pending':0,'shared_layout':True,
            'extra_profiles':2,'core_and_nginx_pids_preserved':True,'default_and_sites_preserved':True,
            'security_generation':self.security()['generation']}
        private_json(self.work/'final-config.json',self.active()[1])

    def close(self):
        self.d._publication_health=self.original_health
        self.store.db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--resume',action='store_true')
    scope=parser.add_mutually_exclusive_group()
    scope.add_argument('--blocker-only',action='store_true',
                       help='Only exercise the P1 safety-intent/stop recovery boundaries with existing A/B')
    scope.add_argument('--owner-only',action='store_true',
                       help='Only exercise durable safety-intent/read-gate ownership with existing A/B')
    parser.add_argument('--child-plan')
    parser.add_argument('--startup-delay',action='store_true')
    parser.add_argument('--crash-point',choices=['after-revocation','after-publish','safety-stop','safety-intent',
                                               'before-gate','safety-gate'])
    args=parser.parse_args()
    if os.name!='posix' or os.geteuid()!=0:
        parser.error('This opt-in acceptance requires the dedicated Linux VPS as root')
    args.work.mkdir(mode=0o700,parents=True,exist_ok=True)
    if args.work.is_symlink():
        parser.error('Private work directory must not be a symlink')
    args.work.chmod(0o700)
    if args.startup_delay:
        (args.work/'startup-ready').touch(mode=0o600)
        deadline=time.monotonic()+30
        while not (args.work/'startup-release').exists():
            if time.monotonic()>deadline:
                return 75
            time.sleep(.05)
        return 0
    if args.child_plan:
        if not args.crash_point:
            parser.error('A child plan requires a controlled crash point')
        crash_child(args)
        return 1
    if args.report is None:
        parser.error('--report is required for the acceptance run')
    if args.resume:
        from proxyhub.core import atomic_write
        for source,name in ((args.report,'report.json'),
                            (args.work/'failure-state.json','failure-state.json'),
                            (args.work/'failure.log','failure.log'),
                            (args.work/'publication-failure.log','publication-failure.log')):
            first=args.work/('first-'+name)
            if source.is_file() and not first.exists():
                atomic_write(first,source.read_bytes(),0o600)
    harness=Acceptance(args)
    result={'schema_version':1,'run_id':harness.state['run_id'],'started_at':utc(),
            'scope':('3a P1 durable-intent/read-gate ownership follow-up only' if args.owner_only
                     else '3a P1 safety-intent/stop blocker follow-up only' if args.blocker_only
                     else '3a shared-layout subscription publication only'),'gui_acceptance':'not_verified',
            'protocol_matrix':'not_repeated','rss_measurements':'external_sampler_required'}
    try:
        if args.resume and harness.store.pending():
            pending=harness.store.pending()
            assert len(pending)==1 and pending[0]['plan_id']==harness.state.get('last_plan')
            harness.mark('resume-known-pending')
            harness.d.recover(harness.store)
        if args.blocker_only or args.owner_only:
            harness.step('owner-baseline' if args.owner_only else 'blocker-baseline',harness.initialize_blockers)
            for kind in ('subscription.token.rotate','subscription.alias.retire'):
                for point in (('before-gate','safety-gate') if args.owner_only else ('safety-stop','safety-intent')):
                    harness.step(kind.rsplit('.',1)[-1]+'-'+point,
                                 lambda kind=kind,point=point:harness.blocker_case(kind,point))
            harness.cleanup_blocker_aliases()
            assert not any(item['id'] in harness.state.get('blocker_aliases',[])
                           for item in harness.profile('A')['aliases'])
            harness.step('prechecked-start-read-gate',harness.startup_gate_boundary)
            if args.blocker_only:
                harness.step('closed-gate-owned-recovery',
                             lambda:harness.startup_gate_boundary(gate_closed=True))
        else:
            harness.step('baseline',harness.initialize)
            harness.step('create-copy-two-profiles',harness.create_profiles)
            harness.step('disable-enable',harness.lifecycle)
            harness.step('host-path-alias-retirement-and-restore',harness.aliases)
            harness.step('concurrent-immutable-publication',harness.concurrent_publication)
            harness.step('management-stopped-last-snapshot',harness.manager_stopped)
            harness.step('invalid-publication-rolls-back',harness.failed_publication)
            harness.step('safety-write-failure-recovery',harness.safety_write_failure)
            harness.step('persistent-failure-pending-recovery',harness.persistent_failure)
            harness.step('process-exit-after-revocation',lambda:harness.crash('after-revocation'))
            harness.step('process-exit-after-publish',lambda:harness.crash('after-publish'))
            harness.step('old-token-restoration-stays-closed',harness.restore_token)
        if args.blocker_only or args.owner_only:
            harness.mark('final-preservation')
            harness.finish()
            harness.event('stage_passed')
        else:
            harness.step('final-preservation',harness.finish)
        result.update(status='passed',checks=harness.state['checks'],
                      concurrent_requests=harness.state.get('concurrent_requests',0),final=harness.state['final'])
    except BaseException as error:
        result.update(status='failed',stage=harness.stage,error_class=type(error).__name__,
                      checks=harness.state['checks'],pending=len(harness.store.pending()))
        private_json(args.work/'failure-state.json',result)
        with (args.work/'failure.log').open('w',encoding='utf-8') as stream:
            os.chmod(args.work/'failure.log',0o600)
            stream.write(traceback.format_exc())
    finally:
        harness.save()
        harness.close()
        result['finished_at']=utc()
        private_json(args.report,result)
    print(json.dumps(result,ensure_ascii=False))
    return int(result['status']!='passed')


if __name__=='__main__':
    raise SystemExit(main())
