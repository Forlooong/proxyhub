import pytest

from proxyhub.cli import main


@pytest.mark.parametrize('arguments', [[], ['help'], ['help','host'], ['help','host','append-key'], ['help','deployment','plan'], ['site'], ['site','certificate'], ['site','content'], ['help','site','content','upload'], ['help','site','content','versions'], ['help','site','content','restore'], ['help','site','content','preview'], ['panel'], ['help','panel','request']])
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
    previous_read = Path.read_text
    def read(path, *args, **kwargs):
        if str(path).replace('\\','/').endswith('/current/config.json'):
            return json.dumps({'server': {'site_domain':'site.example.test','subscription_domain':'sub.example.test','certificate_path':'old'},'subscription':{'token':'old'}})
        return previous_read(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
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
