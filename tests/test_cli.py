import pytest

from proxyhub.cli import main


@pytest.mark.parametrize('arguments', [[], ['help'], ['help','host'], ['help','host','append-key'], ['help','deployment','plan'], ['site'], ['panel'], ['help','panel','request']])
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
