import pytest

from proxyhub.cli import main


@pytest.mark.parametrize('arguments', [[], ['help'], ['help','host'], ['help','host','append-key'], ['help','deployment','plan'], ['site'], ['site','certificate'], ['site','content'], ['help','site','content','upload'], ['help','site','content','versions'], ['help','site','content','restore'], ['help','site','content','preview'], ['site','website'], ['site','website','domain'], ['site','website','certificate'], ['help','site','website','create'], ['help','site','certificate','add'], ['help','site','certificate','request'], ['help','site','certificate','replace'], ['help','site','certificate','renew'], ['help','site','certificate','delete'], ['panel'], ['help','panel','request']])
def test_help_is_available_without_root_or_state(arguments, capsys, monkeypatch):
    monkeypatch.setattr('proxyhub.cli.require_root', lambda: pytest.fail('help must not require root'))
    assert main(arguments) == 0
    assert 'usage:' in capsys.readouterr().out


def test_errors_do_not_echo_secret_runtime_detail(monkeypatch, capsys):
    def fail(_):
        raise OSError('SECRET-TOKEN-and-private-key')
    monkeypatch.setattr('proxyhub.cli.execute', fail)
    assert main(['apply','example']) == 1
    captured = capsys.readouterr()
    assert 'SECRET' not in captured.out + captured.err


def test_unknown_help_fails(capsys):
    with pytest.raises(SystemExit) as result:
        main(['help','unknown'])
    assert result.value.code == 2


def test_ssh_safety_failure_is_redacted(monkeypatch, capsys):
    from proxyhub.host.ssh import SSHSafetyError
    def fail(_):
        raise SSHSafetyError('private authorized key details')
    monkeypatch.setattr('proxyhub.cli.execute', fail)
    assert main(['apply','example']) == 1
    captured = capsys.readouterr()
    assert 'Traceback' not in captured.err
    assert 'private authorized' not in captured.err


def test_renew_cannot_revert_configuration_changed_during_acme(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from proxyhub import cli, certificates
    from proxyhub.core import OperationError
    from proxyhub.state import StateStore
    store = StateStore(tmp_path/'plans')
    monkeypatch.setattr(cli, 'require_root', lambda: None)
    monkeypatch.setattr('proxyhub.state.StateStore', lambda _: store)
    active=tmp_path/'active'
    active.mkdir()
    (active/'config.json').write_text(json.dumps({'server':{'site_domain':'site.example.test',
        'subscription_domain':'sub.example.test','certificate_path':'old'},'subscription':{'token':'old'}}))
    original_resolve=Path.resolve
    def resolve(path,*args,**kwargs):
        return active if path==Path('/var/lib/proxyhub/current') else original_resolve(path,*args,**kwargs)
    monkeypatch.setattr(Path,'resolve',resolve)
    def concurrent_update(*args, **kwargs):
        store.commit(store.plan('ssh', {}))
        return {'certificate_path':'new','key_path':'new-key'}
    monkeypatch.setattr(certificates,'issue',concurrent_update)
    monkeypatch.setattr(certificates,'managed_acme',lambda _: True)
    with pytest.raises(OperationError, match='changed during certificate renewal'):
        cli.execute(cli.parser().parse_args(['deployment','renew']))
    assert store.revision == 1


@pytest.fixture
def content_service(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from proxyhub import cli, management

    class Service:
        def __init__(self):
            self.calls = []
            self.closed = False
            self.result = {}

        @contextmanager
        def state(self):
            yield SimpleNamespace(revision=7)

        def create_plan(self, kind, revision, values):
            self.calls.append((kind, revision, values))
            return {'plan_id': 'a' * 32, 'kind': kind, 'base_revision': revision}

        def handle(self, operation, payload):
            self.calls.append((operation, payload))
            return self.result

        def close(self):
            self.closed = True

    service = Service()
    monkeypatch.setattr(cli, 'require_root', lambda: None)
    def synchronous_service(*, synchronous):
        assert synchronous is True
        return service
    monkeypatch.setattr(management, 'OperationService', synchronous_service)
    return service


@pytest.mark.parametrize('arguments', [
    ['proxy'], ['proxy','reality'], ['help','proxy'], ['help','proxy','reality'],
    ['help','proxy','reality','list'], ['help','proxy','reality','migrate'],
    ['help','proxy','reality','select'], ['help','proxy','reality','retire'],
])
def test_reality_help_does_not_require_root_or_state(arguments, capsys, monkeypatch):
    monkeypatch.setattr('proxyhub.cli.require_root', lambda: pytest.fail('help must not require root'))
    assert main(arguments) == 0
    assert 'usage:' in capsys.readouterr().out


def test_reality_list_uses_read_operation(content_service, capsys):
    import json
    content_service.result = {'revision':7,'node_id':'local-reality','primary_endpoint_id':'original','endpoints':[]}
    assert main(['proxy','reality','list']) == 0
    assert json.loads(capsys.readouterr().out) == content_service.result
    assert content_service.calls == [('reality.resources', {})]
    assert content_service.closed


@pytest.mark.parametrize(('arguments', 'kind', 'values'), [
    (['migrate','--site-id','site-2','--sni','second.example.test'], 'reality.migrate', {'type':'site','site_id':'site-2','sni':'second.example.test'}),
    (['migrate','--external-host','203.0.113.50','--sni','external.example.test'], 'reality.migrate', {'type':'external','address':'203.0.113.50','port':443,'sni':'external.example.test'}),
    (['select','--endpoint-id','original'], 'reality.select', {'endpoint_id':'original'}),
    (['retire','--endpoint-id','original'], 'reality.retire', {'endpoint_id':'original'}),
])
def test_reality_changes_prepare_without_applying(arguments, kind, values, content_service, capsys):
    import json
    assert main(['proxy','reality',*arguments]) == 0
    assert json.loads(capsys.readouterr().out)['plan_id'] == 'a' * 32
    assert content_service.calls == [(kind, 7, values)]
    assert content_service.closed


@pytest.mark.parametrize('arguments', [
    ['migrate','--sni','second.example.test'],
    ['migrate','--site-id','site-2','--external-host','external.example.test','--sni','second.example.test'],
    ['migrate','--site-id','site-2'],
    ['migrate','--external-host','external.example.test'],
    ['select'], ['retire'],
])
def test_reality_parser_requires_explicit_target_sni_and_entry(arguments, monkeypatch):
    monkeypatch.setattr('proxyhub.cli.execute', lambda _: pytest.fail('invalid command must not open state'))
    with pytest.raises(SystemExit) as result:
        main(['proxy','reality',*arguments])
    assert result.value.code == 2


def test_reality_failure_closes_service_and_redacts_details(content_service, capsys):
    def failed(*args):
        raise OSError('PRIVATE-REALITY-DETAIL')
    content_service.create_plan = failed
    assert main(['proxy','reality','retire','--endpoint-id','original']) == 1
    captured = capsys.readouterr()
    assert 'PRIVATE-REALITY-DETAIL' not in captured.out + captured.err
    assert content_service.closed


def test_content_upload_prepares_with_current_revision(tmp_path, content_service, capsys):
    import base64
    import json
    from zipfile import ZipFile
    archive = tmp_path / 'website.zip'
    with ZipFile(archive, 'w') as output:
        output.writestr('index.html', '<h1>Website</h1>')
    assert main(['site','content','upload','--archive',str(archive)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview == {'plan_id': 'a' * 32, 'kind': 'website.content.upload', 'base_revision': 7}
    kind, revision, values = content_service.calls[0]
    assert kind == 'website.content.upload' and revision == 7
    assert base64.b64decode(values['archive_base64']) == archive.read_bytes()
    assert content_service.closed


@pytest.mark.parametrize('size', [0, 180 * 1024 + 1])
def test_content_upload_rejects_empty_or_oversized_archives(tmp_path, content_service, capsys, size):
    archive = tmp_path / 'website.zip'
    archive.write_bytes(b'x' * size)
    assert main(['site','content','upload','--archive',str(archive)]) == 1
    assert content_service.calls == [] and content_service.closed
    assert str(archive) not in capsys.readouterr().err


@pytest.mark.parametrize('version', ['template', 'b' * 32])
def test_content_restore_prepares_template_or_version(content_service, capsys, version):
    assert main(['site','content','restore','--version',version]) == 0
    assert content_service.calls == [('website.content.restore', 7, {'version_id': version})]
    assert content_service.closed


def test_content_versions_uses_read_rpc(content_service, capsys):
    import json
    content_service.result = {'current_version': None, 'versions': [{'id': 'template', 'current': True}]}
    assert main(['site','content','versions']) == 0
    assert json.loads(capsys.readouterr().out) == content_service.result
    assert content_service.calls == [('website.content.versions', {})]
    assert content_service.closed


def test_content_preview_writes_private_html_without_echoing_it(tmp_path, content_service, capsys, monkeypatch):
    import json
    import os
    from proxyhub import cli
    output = tmp_path / 'preview.html'
    html = '<!doctype html><html><body>预览 PRIVATE-HTML-CANARY</body></html>'
    content_service.result = {'html': html, 'file_count': 2, 'total_bytes': 128}
    write = cli.atomic_write
    modes = []
    def private_write(path, content, mode):
        modes.append(mode)
        write(path, content, mode)
    monkeypatch.setattr(cli, 'atomic_write', private_write)
    assert main(['site','content','preview','--plan-id','a'*32,'--output',str(output)]) == 0
    captured = capsys.readouterr()
    assert 'PRIVATE-HTML-CANARY' not in captured.out + captured.err
    assert json.loads(captured.out) == {'status': 'written', 'private_preview': str(output), 'file_count': 2, 'total_bytes': 128}
    assert output.read_text(encoding='utf-8') == html
    assert modes == [0o600]
    if os.name == 'posix':
        assert output.stat().st_mode & 0o777 == 0o600
    assert content_service.calls == [('website.content.preview', {'plan_id': 'a'*32})]
    assert content_service.closed


def test_content_failure_closes_service_and_redacts_error(content_service, capsys):
    def fail(*args):
        raise OSError('PRIVATE-HTML-CANARY')
    content_service.handle = fail
    assert main(['site','content','versions']) == 1
    captured = capsys.readouterr()
    assert 'PRIVATE-HTML-CANARY' not in captured.out + captured.err
    assert content_service.closed


def test_content_commands_still_require_root(monkeypatch, capsys):
    from proxyhub import cli, management
    from proxyhub.core import OperationError
    def denied():
        raise OperationError('root required')
    monkeypatch.setattr(cli, 'require_root', denied)
    monkeypatch.setattr(management, 'OperationService', lambda: pytest.fail('root required before opening state'))
    assert main(['site','content','versions']) == 1
    assert 'OperationError' in capsys.readouterr().err


@pytest.mark.parametrize(('arguments', 'kind', 'values'), [
    (['website','create','--title','Second site','--domain','second.example.test','--certificate-id','cert-2'], 'website.create', {'title':'Second site','domain':'second.example.test','certificate_id':'cert-2'}),
    (['website','enable','--site-id','site-2'], 'website.enable', {'site_id':'site-2'}),
    (['website','disable','--site-id','site-2'], 'website.disable', {'site_id':'site-2'}),
    (['website','delete','--site-id','site-2'], 'website.delete', {'site_id':'site-2'}),
    (['website','domain','bind','--site-id','site-2','--domain','alias.example.test'], 'website.domain.bind', {'site_id':'site-2','domain':'alias.example.test'}),
    (['website','domain','unbind','--site-id','site-2','--domain','alias.example.test'], 'website.domain.unbind', {'site_id':'site-2','domain':'alias.example.test'}),
    (['website','certificate','bind','--site-id','site-2','--certificate-id','cert-2'], 'website.certificate.bind', {'site_id':'site-2','certificate_id':'cert-2'}),
    (['certificate','request','--name','Second certificate','--domain','second.example.test','--domain','alias.example.test'], 'certificate.issue', {'name':'Second certificate','domains':['second.example.test','alias.example.test']}),
    (['certificate','renew','--certificate-id','cert-2'], 'certificate.renew', {'certificate_id':'cert-2'}),
    (['certificate','delete','--certificate-id','cert-2'], 'certificate.delete', {'certificate_id':'cert-2'}),
    (['content','restore','--site-id','site-2','--version','template'], 'website.content.restore', {'site_id':'site-2','version_id':'template'}),
])
def test_resource_changes_prepare_with_current_revision(arguments, kind, values, content_service, capsys):
    import json
    assert main(['site', *arguments]) == 0
    assert json.loads(capsys.readouterr().out)['plan_id'] == 'a' * 32
    assert content_service.calls == [(kind, 7, values)]
    assert content_service.closed


@pytest.mark.parametrize('resource', ['website', 'certificate'])
def test_resource_lists_use_read_operation(resource, content_service, capsys):
    import json
    content_service.result = {'revision':7,'websites':[],'certificates':[]}
    assert main(['site',resource,'list']) == 0
    assert json.loads(capsys.readouterr().out) == content_service.result
    assert content_service.calls == [('web.resources', {})]
    assert content_service.closed


def test_content_versions_can_select_site(content_service, capsys):
    assert main(['site','content','versions','--site-id','site-2']) == 0
    assert content_service.calls == [('website.content.versions', {'site_id':'site-2'})]


@pytest.mark.parametrize('action', ['add', 'replace'])
def test_certificate_files_prepare_without_echoing_pem(tmp_path, content_service, capsys, action):
    certificate = tmp_path / 'certificate.pem'
    key = tmp_path / 'private-key.pem'
    certificate.write_text('CERTIFICATE-PEM-CANARY', encoding='utf-8')
    key.write_text('PRIVATE-KEY-PEM-CANARY', encoding='utf-8')
    arguments = ['site','certificate',action,'--certificate',str(certificate),'--key',str(key)]
    values = {'certificate_pem':'CERTIFICATE-PEM-CANARY','private_key_pem':'PRIVATE-KEY-PEM-CANARY'}
    if action == 'add':
        arguments += ['--name','Second certificate','--domain','second.example.test']
        values.update(name='Second certificate', domains=['second.example.test'])
    else:
        arguments += ['--certificate-id','cert-2']
        values['certificate_id'] = 'cert-2'
    assert main(arguments) == 0
    captured = capsys.readouterr()
    assert 'PEM-CANARY' not in captured.out + captured.err
    assert content_service.calls == [('certificate.add' if action == 'add' else 'certificate.import',7,values)]
    assert content_service.closed


@pytest.mark.parametrize(('field', 'size'), [('certificate',0), ('certificate',100*1024+1), ('key',0), ('key',100*1024+1)])
def test_certificate_files_are_bounded_before_plan(tmp_path, content_service, capsys, field, size):
    certificate = tmp_path / 'certificate.pem'
    key = tmp_path / 'private-key.pem'
    certificate.write_bytes(b'CERTIFICATE-PEM-CANARY')
    key.write_bytes(b'PRIVATE-KEY-PEM-CANARY')
    (certificate if field == 'certificate' else key).write_bytes(b'x' * size)
    assert main(['site','certificate','replace','--certificate-id','cert-2','--certificate',str(certificate),'--key',str(key)]) == 1
    assert content_service.calls == [] and content_service.closed
    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.err and 'PEM-CANARY' not in captured.out + captured.err


def test_apply_panel_renew_uses_synchronous_service(content_service, monkeypatch, capsys):
    from types import SimpleNamespace
    monkeypatch.setattr('proxyhub.state.StateStore', lambda _: SimpleNamespace(get=lambda identity: ('panel-renew', {})))
    calls = []
    content_service.apply_plan = lambda identity: calls.append(identity) or {'status':'unchanged'}
    assert main(['apply','a'*32]) == 0
    assert calls == ['a'*32] and content_service.closed


@pytest.mark.parametrize('action', ['issue', 'import'])
def test_original_certificate_commands_keep_original_behavior(tmp_path, monkeypatch, capsys, action):
    from proxyhub import certificates
    monkeypatch.setattr('proxyhub.cli.require_root', lambda: None)
    calls = []
    arguments = ['site','certificate',action,'--domain','original.example.test']
    if action == 'issue':
        monkeypatch.setattr(certificates,'issue',lambda domains, *, standalone: calls.append((domains,standalone)) or {'status':'issued'})
        assert main(arguments) == 0
        assert calls == [(['original.example.test'], True)]
    else:
        certificate, key = tmp_path/'certificate.pem', tmp_path/'key.pem'
        arguments += ['--certificate',str(certificate),'--key',str(key)]
        monkeypatch.setattr(certificates,'import_pair',lambda cert, pem, domains: calls.append((cert,pem,domains)) or {'status':'imported'})
        assert main(arguments) == 0
        assert calls == [(certificate,key,['original.example.test'])]


@pytest.fixture
def web_renewal_service(content_service, monkeypatch):
    import json
    from contextlib import contextmanager
    from pathlib import Path
    from types import SimpleNamespace
    from proxyhub import certificates, websites

    store = SimpleNamespace(revision=7, pending_records=[])
    store.pending = lambda: store.pending_records
    monkeypatch.setattr('proxyhub.state.StateStore', lambda _: store)
    monkeypatch.setattr(websites, 'normalize_web', lambda _: {'certificates':[{'id':identity} for identity in ('first','second','third')]})
    monkeypatch.setattr(certificates, 'acme_source', lambda binding: {'lineage':binding['id']})
    read_text = Path.read_text
    def read(path, *args, **kwargs):
        if str(path).replace('\\','/').endswith('/current/config.json'):
            return json.dumps({'web':{}})
        return read_text(path,*args,**kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    @contextmanager
    def state():
        yield store
    content_service.state = state
    return content_service, store


@pytest.mark.parametrize('failure_stage', ['prepare','apply'])
def test_web_renewal_continues_independent_lineages_after_failure(web_renewal_service, capsys, failure_stage):
    import json
    from proxyhub.core import OperationError
    service, store = web_renewal_service
    create_plan = service.create_plan
    def prepare(kind, revision, values):
        result = create_plan(kind,revision,values)
        if failure_stage == 'prepare' and values['certificate_id'] == 'first':
            raise OperationError('SECRET-DNS-OR-CERTBOT-DETAIL')
        return result
    def apply(identity):
        if service.calls[-1][2]['certificate_id'] == 'first':
            raise OperationError('SECRET-DNS-OR-CERTBOT-DETAIL')
        store.revision += 1
        return {'status':'unchanged'}
    service.create_plan, service.apply_plan = prepare, apply
    assert main(['deployment','renew']) == 1
    captured = capsys.readouterr()
    assert 'SECRET' not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result['status'] == 'partial_failure'
    assert result['certificates'] == [
        {'certificate_id':'first','status':'failed','error':{'code':'VALIDATION'}},
        {'certificate_id':'second','status':'unchanged'},
        {'certificate_id':'third','status':'unchanged'},
    ]
    assert [(values['certificate_id'],revision) for _,revision,values in service.calls] == [('first',7),('second',7),('third',8)]
    assert service.closed


@pytest.mark.parametrize('pending_before', [True, False])
def test_web_renewal_stops_when_activation_requires_recovery(web_renewal_service, capsys, pending_before):
    import json
    from proxyhub.core import OperationError
    service, store = web_renewal_service
    if pending_before:
        store.pending_records.append({'plan_id':'interrupted'})
    def apply(identity):
        store.pending_records.append({'plan_id':'interrupted'})
        raise OperationError('SECRET-ROLLBACK-DETAIL')
    service.apply_plan = apply
    assert main(['deployment','renew']) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert 'SECRET' not in captured.out + captured.err
    assert result['status'] == 'failed' and result['recovery_required'] is True
    assert len(result['certificates']) == 1
    assert len(service.calls) == (0 if pending_before else 1)
    assert service.closed


def test_web_renewal_reports_failure_after_checking_all_independent_lineages(web_renewal_service, capsys):
    import json
    from proxyhub.core import OperationError
    service, _ = web_renewal_service
    def apply(identity):
        raise OperationError('SECRET-CERTBOT-DETAIL')
    service.apply_plan = apply
    assert main(['deployment','renew']) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result['status'] == 'failed'
    assert len(result['certificates']) == len(service.calls) == 3
    assert 'SECRET' not in captured.out + captured.err
    assert service.closed
