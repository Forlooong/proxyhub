"""Independent protocol transactions preserve existing listeners and credentials."""
from copy import deepcopy
import errno
import json
import socket
import sys

import pytest

from proxyhub import deployment, nodes
from proxyhub.core import OperationError
from proxyhub.websites import get_certificate
from test_hy2_activation import hy2_environment, create as create_hy2
from test_multisite_activation import multisite, CERTIFICATE
from test_subscription_activation import environment, read_config


def prepare(env, config, kind, identity):
    result = deployment.prepare(env.store,config,node_operation={'kind':kind,'node_id':identity})
    return result, env.root/'revisions'/result['revision']


def add(env, *, protocol='ss', name='Selected SS', port=5443, options=None):
    before = read_config(env.pointer['current'])
    config = nodes.change(before,'nodes.create',{'protocol':protocol,'name':name,'port':port,
        'options':options or ({'method':'aes-256-gcm'} if protocol == 'ss' else {})})
    identity = config['nodes'][-1]['id']
    prepared, directory = prepare(env,config,'create',identity)
    deployment.apply(env.store,prepared['plan_id'])
    env.calls.clear()
    env.node_health.clear()
    return read_config(directory), identity


@pytest.fixture
def node_environment(hy2_environment,monkeypatch):
    env = hy2_environment
    env.node_health = []
    env.real_check_node_ports = deployment._check_node_ports
    monkeypatch.setattr(deployment,'_check_node_ports',lambda config,previous:None)
    monkeypatch.setattr(deployment,'_probe_node_targets',lambda config,identities:None)
    def health(config,directory,identities=None):
        if identities is not None and not identities:
            return
        env.node_health.append((deepcopy(config),directory,set(identities) if identities is not None else None))
    monkeypatch.setattr(deployment,'_wait_nodes_health',health)
    create_hy2(env)
    _, env.retained_id = add(env,name='Retained SS',port=5444)
    env.previous = env.pointer['current']
    return env


def unit(identity):
    return 'proxyhub-node@'+identity+'.service'


def assert_only_selected_node(env,identity,*,web=False):
    allowed = {unit(identity),'proxyhub-publisher.service'}
    if web:
        allowed.add('proxyhub-nginx.service')
    for call in env.calls:
        if call[0] == 'systemctl':
            assert set(call[2:]) <= allowed


@pytest.mark.parametrize('kind,action',[('create','start'),('update','restart'),('disable','stop'),('enable','start'),('delete',None)])
def test_typed_lifecycle_changes_one_instance_and_publication(node_environment,kind,action):
    env = node_environment
    before = read_config(env.previous)
    if kind == 'create':
        config = nodes.change(before,'nodes.create',{'protocol':'ss','name':'Selected SS','port':5443,'options':{}})
        identity = config['nodes'][-1]['id']
    else:
        before, identity = add(env)
        if kind in {'enable','delete'}:
            disabled = nodes.change(before,'nodes.disable',{'node_id':identity})
            preview, _ = prepare(env,disabled,'disable',identity)
            deployment.apply(env.store,preview['plan_id'])
            before = read_config(env.pointer['current'])
        config = nodes.change(before,'nodes.'+kind,{'node_id':identity,**({'port':5445} if kind == 'update' else {})})
    prepared, directory = prepare(env,config,kind,identity)
    payload = env.store.inspect(prepared['plan_id'])['payload']
    assert payload['activation'] == 'node'
    assert payload['node_operation'] == {'kind':kind,'node_id':identity}
    if kind != 'delete':
        assert 'nodes/'+identity+'.json' in payload['files']
        rendered = json.loads((directory/'nodes'/(identity+'.json')).read_text())
        assert rendered['inbounds'][0]['password'] == nodes.get_node(config['nodes'],identity)['credentials']['password']
    env.calls.clear()
    env.node_health.clear()
    assert deployment.apply(env.store,prepared['plan_id'])['activation'] == 'node'
    if action:
        assert ('systemctl',action,unit(identity)) in env.calls
    assert ('systemctl','reload','proxyhub-publisher.service') in env.calls
    assert_only_selected_node(env,identity)
    current = read_config(directory)
    assert all(current[key] == before[key] for key in ('server','web','subscription','overrides','hy2'))
    assert nodes.get_node(current['nodes'],env.retained_id) == nodes.get_node(before['nodes'],env.retained_id)
    if kind not in {'create','delete'}:
        assert nodes.get_node(current['nodes'],identity)['credentials'] == nodes.get_node(before['nodes'],identity)['credentials']
    assert env.store.pending() == []


@pytest.mark.parametrize('mode',['full','web','publication'])
def test_raw_activation_cannot_change_node_registry(node_environment,mode):
    env = node_environment
    config = nodes.change(read_config(env.previous),'nodes.update',{'node_id':env.retained_id,'name':'Unapproved node change'})
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.prepare(env.store,config,web_only=mode == 'web',publish_only=mode == 'publication')
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


@pytest.mark.parametrize('mutation',['credentials','other-node','hy2','website','subscription'])
def test_typed_update_rejects_changes_outside_selected_node(node_environment,mutation):
    env = node_environment
    before, identity = add(env)
    config = nodes.change(before,'nodes.update',{'node_id':identity,'name':'Selected rename'})
    if mutation == 'credentials':
        nodes.get_node(config['nodes'],identity)['credentials']['password'] = 'x'*43
    elif mutation == 'other-node':
        nodes.get_node(config['nodes'],env.retained_id)['name'] = 'Unrelated node change'
    elif mutation == 'hy2':
        config['hy2']['name'] = 'Unrelated HY2'
    elif mutation == 'website':
        config['web']['sites'][1]['title'] = 'Unrelated website'
    else:
        config['subscription']['name'] = 'Unrelated publication'
    previous = env.pointer['current']
    env.calls.clear()
    with pytest.raises(OperationError):
        prepare(env,config,'update',identity)
    assert env.calls == [] and env.pointer['current'] == previous and env.store.pending() == []


def test_typed_delete_cannot_remove_enabled_listener(node_environment):
    env = node_environment
    config = read_config(env.previous)
    config['nodes'] = []
    env.calls.clear()
    with pytest.raises(OperationError):
        prepare(env,config,'delete',env.retained_id)
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


@pytest.mark.parametrize('mutation',['changed','missing','manifest','extra'])
def test_extra_runtime_inputs_are_verified_before_service_changes(node_environment,mutation):
    env = node_environment
    config = nodes.change(read_config(env.previous),'nodes.update',{'node_id':env.retained_id,'name':'Selected rename'})
    prepared, directory = prepare(env,config,'update',env.retained_id)
    identity = prepared['plan_id']
    path = directory/'nodes'/(env.retained_id+'.json')
    if mutation == 'changed':
        path.write_text('{}')
    elif mutation == 'missing':
        path.unlink()
    elif mutation == 'extra':
        (path.parent/('f'*32+'.json')).write_text('{}')
    else:
        payload = env.store.inspect(identity)['payload']
        payload['files'].pop('nodes/'+env.retained_id+'.json')
        identity = env.store.plan('deployment',payload)
    env.calls.clear()
    with pytest.raises(OperationError):
        deployment.apply(env.store,identity)
    assert env.calls == [] and env.pointer['current'] == env.previous and env.store.pending() == []


@pytest.mark.parametrize('stage,network',[('prepare','tcp'),('apply','udp')])
def test_occupied_tcp_or_udp_port_is_rejected_before_switch(node_environment,monkeypatch,stage,network):
    env = node_environment
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM if network == 'tcp' else socket.SOCK_DGRAM) as occupied:
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            occupied.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        occupied.bind(('0.0.0.0',0))
        if network == 'tcp':
            occupied.listen()
        config = nodes.change(read_config(env.previous),'nodes.create',{'protocol':'ss','name':'Port conflict','port':occupied.getsockname()[1],'options':{}})
        identity = config['nodes'][-1]['id']
        if stage == 'apply':
            prepared, _ = prepare(env,config,'create',identity)
        monkeypatch.setattr(deployment,'_check_node_ports',env.real_check_node_ports)
        env.calls.clear()
        with pytest.raises(OperationError,match='unavailable'):
            if stage == 'prepare':
                prepare(env,config,'create',identity)
            else:
                deployment.apply(env.store,prepared['plan_id'])
    assert not any(call[0] in ('systemctl','publish') for call in env.calls)
    assert env.pointer['current'] == env.previous and env.store.pending() == []


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='Linux TCP TIME_WAIT and SO_REUSEADDR semantics')
def test_recently_stopped_tcp_listener_can_reenable_but_active_listener_still_conflicts(node_environment):
    env = node_environment
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',0))
        port = listener.getsockname()[1]
        listener.listen()
        with socket.create_connection(('127.0.0.1',port),timeout=2) as client:
            server, _ = listener.accept()
            with server:
                server.settimeout(2)
                listener.close()
                # The listener side closes first, so its port retains TIME_WAIT.
                server.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b''
                client.close()
                assert server.recv(1) == b''
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as non_reusing:
        with pytest.raises(OSError) as failure:
            non_reusing.bind(('0.0.0.0',port))
        assert failure.value.errno == errno.EADDRINUSE
    config = nodes.change(read_config(env.previous),'nodes.create',{'protocol':'vless','name':'TIME_WAIT candidate',
        'port':port,'options':{'transport':'tcp','tls':False}})
    env.real_check_node_ports(config,env.previous)
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as active:
        active.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        active.bind(('0.0.0.0',port))
        active.listen()
        with pytest.raises(OperationError,match='unavailable'):
            env.real_check_node_ports(config,env.previous)


def test_certificate_replacement_restarts_only_selected_binding_consumer(node_environment):
    env = node_environment
    before, identity = add(env,protocol='trojan',name='Certificate Trojan',options={'sni':'second.example.test','certificate_id':CERTIFICATE})
    config = deepcopy(before)
    binding = get_certificate(config['web'],CERTIFICATE)
    binding.update(certificate_path='/var/lib/proxyhub/certificates/87654321/fullchain.pem',key_path='/var/lib/proxyhub/certificates/87654321/privkey.pem')
    prepared = deployment.prepare(env.store,config,web_only=True)
    directory = env.root/'revisions'/prepared['revision']
    rendered = json.loads((directory/'nodes'/(identity+'.json')).read_text())['inbounds'][0]['tls']
    assert rendered['certificate'] == ['CERTIFICATE-FIXTURE',binding['certificate_path']]
    assert rendered['key'] == ['KEY-FIXTURE',binding['key_path']]
    env.calls.clear()
    deployment.apply(env.store,prepared['plan_id'])
    assert ('systemctl','restart',unit(identity)) in env.calls
    # HY2 intentionally uses the same second binding and must also refresh.
    assert ('systemctl','restart','proxyhub-hy2.service') in env.calls
    assert not any(unit(env.retained_id) in call for call in env.calls)
    assert not any('restart' in call and any(name.startswith(('proxyhub-proxy.','proxyhub-reality@','proxyhub-nginx.')) for name in call) for call in env.calls)
    assert read_config(directory)['nodes'] == before['nodes']


@pytest.mark.parametrize('mode',['web','publication'])
def test_unrelated_changes_do_not_restart_or_probe_nodes(node_environment,mode):
    env = node_environment
    config = read_config(env.previous)
    if mode == 'web':
        config['web']['sites'][1]['title'] = 'Content title'
    else:
        config['subscription']['name'] = 'Metadata update'
    prepared = deployment.prepare(env.store,config,web_only=mode == 'web',publish_only=mode == 'publication')
    env.calls.clear()
    env.node_health.clear()
    deployment.apply(env.store,prepared['plan_id'])
    assert not any(any(str(value).startswith('proxyhub-node@') for value in call) for call in env.calls)
    assert not env.node_health


def test_failed_selected_health_restores_old_config_and_credentials(node_environment,monkeypatch):
    env = node_environment
    before = read_config(env.previous)
    config = nodes.change(before,'nodes.update',{'node_id':env.retained_id,'port':5445})
    prepared, candidate = prepare(env,config,'update',env.retained_id)
    observed = []
    def health(snapshot,directory,identities=None):
        observed.append(directory)
        assert env.pointer['current'] == directory
        if directory == candidate:
            raise OperationError('Selected node did not authenticate')
        assert snapshot == before
    monkeypatch.setattr(deployment,'_wait_nodes_health',health)
    env.calls.clear()
    with pytest.raises(OperationError,match='did not authenticate'):
        deployment.apply(env.store,prepared['plan_id'])
    assert observed == [candidate,env.previous]
    assert env.pointer['current'] == env.previous and read_config(env.previous) == before
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed' and env.store.pending() == []
    assert_only_selected_node(env,env.retained_id)


def test_failed_restore_keeps_pending_until_previous_authenticates(node_environment,monkeypatch):
    env = node_environment
    config = nodes.change(read_config(env.previous),'nodes.update',{'node_id':env.retained_id,'port':5445})
    prepared, _ = prepare(env,config,'update',env.retained_id)
    def unavailable(*args,**kwargs):
        raise OperationError('Node authentication unavailable')
    monkeypatch.setattr(deployment,'_wait_nodes_health',unavailable)
    with pytest.raises(OperationError):
        deployment.apply(env.store,prepared['plan_id'])
    assert env.store.pending()
    with pytest.raises(OperationError):
        deployment.recover(env.store)
    assert env.store.pending()
    monkeypatch.setattr(deployment,'_wait_nodes_health',lambda *args,**kwargs:None)
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == env.previous and env.store.pending() == []


@pytest.mark.parametrize('tamper',[False,True])
def test_interrupted_node_activation_recovery_checks_candidate_before_mutating_services(node_environment,tamper):
    env = node_environment
    config = nodes.change(read_config(env.previous),'nodes.disable',{'node_id':env.retained_id})
    prepared, candidate = prepare(env,config,'disable',env.retained_id)
    env.store.record(prepared['plan_id'],'activating',{'previous':str(env.previous),'candidate':str(candidate),'activation':'node'})
    env.pointer['current'] = candidate
    if tamper:
        (candidate/'nodes'/(env.retained_id+'.json')).write_text('{}')
    revision = env.store.revision
    env.calls.clear()
    if tamper:
        with pytest.raises(OperationError):
            deployment.recover(env.store)
        assert env.calls == [] and env.store.pending()
        return
    assert deployment.recover(env.store)['status'] == 'recovered'
    assert env.pointer['current'] == env.previous and env.store.pending() == [] and env.store.revision == revision
    assert env.store.inspect(prepared['plan_id'])['status'] == 'failed'
    assert_only_selected_node(env,env.retained_id)
    env.calls.clear()
    assert deployment.recover(env.store)['status'] == 'nothing_to_recover' and env.calls == []
