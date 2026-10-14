from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxyhub import panel_install as panel
from proxyhub.core import OperationError


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    prefix = tmp_path / "opt/proxyhub"
    prefix.mkdir(parents=True)
    (prefix / ".proxyhub-owned").write_text("owned")
    assets = prefix / "app/packages/panel/dist"
    assets.mkdir(parents=True)
    (assets / "index.html").write_text("panel")
    for module in ("fastapi", "uvicorn"):
        target = prefix / "panel-python" / module
        target.mkdir(parents=True)
        (target / "__init__.py").write_text("")
    data = tmp_path / "data"
    data.mkdir()
    units = tmp_path / "units"
    units.mkdir()
    runtime = tmp_path / "run"
    identities = [None, None]
    calls, ownership = [], []
    def command(argv):
        calls.append(argv)
        if argv[0] == "useradd":
            identities[:] = [SimpleNamespace(pw_uid=987, pw_gid=986, pw_shell="/usr/sbin/nologin",
                pw_dir="/nonexistent", pw_gecos="ProxyHub management gateway"),
                SimpleNamespace(gr_gid=986, gr_mem=[])]
        return ""
    monkeypatch.setattr(panel, "PREFIX", prefix)
    monkeypatch.setattr(panel, "PANEL", data / "panel")
    monkeypatch.setattr(panel, "UNIT_DIR", units)
    monkeypatch.setattr(panel, "RUNTIME", runtime)
    monkeypatch.setattr(panel, "require_root", lambda: None)
    monkeypatch.setattr(panel, "run", command)
    monkeypatch.setattr(panel, "_identity", lambda: tuple(identities))
    monkeypatch.setattr(panel.os, "chown", lambda *args: ownership.append(args), raising=False)
    monkeypatch.setattr(panel.os, "getgrouplist", lambda *_: [986], raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.20 57000 198.51.100.10 33623")
    return SimpleNamespace(root=tmp_path, prefix=prefix, data=data, units=units, calls=calls,
                           identities=identities, ownership=ownership)


def test_install_uses_only_namespaced_units_and_never_returns_the_credential(sandbox):
    result = panel.install()
    credential = (panel.PANEL / "credential").read_text().strip()
    config = json.loads((panel.PANEL / "gateway.json").read_text())
    assert credential not in json.dumps(result)
    assert config["credential_sha256"] == hashlib.sha256(credential.encode()).hexdigest()
    assert credential not in json.dumps(config)
    assert result["loopback_only"] and result["credential_created"]
    assert set(path.name for path in sandbox.units.iterdir()) == set(panel.UNIT_NAMES)
    service_calls = [argv for argv in sandbox.calls if argv[0] == "systemctl"]
    assert ["systemctl", "restart", *panel.UNIT_NAMES] in service_calls
    assert all("proxyhub-proxy.service" not in argv and "proxyhub-nginx.service" not in argv for argv in service_calls)
    assert (panel.PANEL, 0, 986) in sandbox.ownership
    assert (panel.PANEL / "gateway.json", 0, 986) in sandbox.ownership


def test_install_is_repeatable_and_does_not_rotate_the_access_credential(sandbox):
    panel.install()
    original = (panel.PANEL / "credential").read_bytes()
    result = panel.install()
    assert result["credential_created"] is False
    assert (panel.PANEL / "credential").read_bytes() == original
    assert len([argv for argv in sandbox.calls if argv[0] == "useradd"]) == 1


def test_access_is_an_explicit_root_only_operation(sandbox, monkeypatch):
    panel.install()
    value = panel.access()
    assert value["credential"] == (panel.PANEL / "credential").read_text().strip()
    assert "-p 33623 root@198.51.100.10" in value["ssh_tunnel"]
    assert value["url"] == "http://127.0.0.1:18765"
    def deny():
        raise OperationError("root required")
    monkeypatch.setattr(panel, "require_root", deny)
    with pytest.raises(OperationError, match="root"):
        panel.access()


def test_keeps_existing_ssh_context_when_not_inside_ssh(sandbox, monkeypatch):
    panel.install()
    original = (panel.PANEL / "ssh-context.json").read_bytes()
    monkeypatch.delenv("SSH_CONNECTION")
    assert panel.install()["ssh_context_captured"] is False
    assert (panel.PANEL / "ssh-context.json").read_bytes() == original


@pytest.mark.parametrize("value", ["not an ssh context", "x 123 198.51.100.10 33623", "192.0.2.1 22 198.51.100.10 0"])
def test_invalid_context_fails_before_system_mutation(sandbox, monkeypatch, value):
    monkeypatch.setenv("SSH_CONNECTION", value)
    with pytest.raises(OperationError, match="SSH_CONNECTION"):
        panel.install()
    assert sandbox.calls == []


def test_unknown_existing_panel_account_is_not_taken_over(sandbox):
    sandbox.identities[0] = SimpleNamespace(pw_uid=999)
    with pytest.raises(OperationError, match="not owned"):
        panel.install()
    assert sandbox.calls == []


def test_unknown_existing_panel_directory_is_not_taken_over(sandbox):
    panel.PANEL.mkdir()
    (panel.PANEL / "unrelated").write_text("data")
    with pytest.raises((OperationError, FileNotFoundError)):
        panel.install()
    assert sandbox.calls == []
    assert (panel.PANEL / "unrelated").read_text() == "data"


def test_unowned_unit_is_not_overwritten(sandbox):
    unit = sandbox.units / panel.UNIT_NAMES[0]
    unit.write_text("[Service]\nExecStart=/other/program\n")
    with pytest.raises(OperationError, match="not owned"):
        panel.install()
    assert unit.read_text().startswith("[Service]") and sandbox.calls == []


def test_account_identity_drift_is_not_accepted(sandbox):
    panel.install()
    sandbox.calls.clear()
    sandbox.identities[0].pw_uid += 1
    with pytest.raises(OperationError, match="identity"):
        panel.install()
    assert sandbox.calls == []


def test_additional_group_memberships_are_rejected(sandbox, monkeypatch):
    panel.install()
    if os.name != "posix":
        pytest.skip("Supplementary groups are POSIX identities")
    monkeypatch.setattr(panel.os, "getgrouplist", lambda *_: [986, 0])
    with pytest.raises(OperationError, match="supplementary"):
        panel.install()


def test_gateway_hash_drift_requires_operator_attention(sandbox):
    panel.install()
    saved = panel.PANEL / "gateway.json"
    config = json.loads(saved.read_text())
    config["credential_sha256"] = "0" * 64
    saved.write_text(json.dumps(config))
    with pytest.raises(OperationError, match="digest"):
        panel.install()


def test_assets_and_pinned_dependencies_are_required_first(sandbox):
    (sandbox.prefix / "panel-python/fastapi/__init__.py").unlink()
    with pytest.raises(OperationError, match="dependencies"):
        panel.install()
    assert sandbox.calls == []


def test_generated_units_keep_gateway_unprivileged_and_runtime_directory(sandbox):
    manager, gateway = panel.units(987, 986).values()
    assert "User=root" in manager and "--gateway-uid 987 --socket-gid 986" in manager
    assert "RuntimeDirectoryPreserve=yes" in manager
    assert "ReadWritePaths=/etc/ssh /etc/systemd/system /var/lib/proxyhub /run/proxyhub" in manager
    assert "User=proxyhub-panel" in gateway and "Group=proxyhub-panel" in gateway
    assert "ProtectHome=yes" in gateway and "ProtectSystem=strict" in gateway
    assert "CapabilityBoundingSet=\n" in gateway
    assert "PYTHONPATH=/opt/proxyhub/app/src:/opt/proxyhub/panel-python" in gateway
    assert "InaccessiblePaths=/var/lib/proxyhub/panel/credential" in gateway
    assert "StandardOutput=null" in manager and "StandardOutput=null" in gateway


def test_panel_symlink_destination_is_refused(sandbox):
    target = sandbox.root / "other"
    target.mkdir()
    try:
        panel.PANEL.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Platform does not allow symlink creation")
    with pytest.raises(OperationError, match="symlink"):
        panel.install()
    assert not list(target.iterdir()) and sandbox.calls == []
