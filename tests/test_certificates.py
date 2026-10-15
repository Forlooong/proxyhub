"""Certificate imports must never modify an existing published revision."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import subprocess

import pytest

from proxyhub import certificates
from proxyhub.core import OperationError


@pytest.fixture(scope="module")
def pairs(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL is required for certificate import integration tests")
    directory = tmp_path_factory.mktemp("certificate-inputs")
    result = []
    for number in range(2):
        cert, key = directory / f"cert-{number}.pem", directory / f"key-{number}.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=site.example.test", "-addext", "subjectAltName=DNS:site.example.test",
        ], check=True, capture_output=True, timeout=20)
        result.append((cert.read_bytes(), key.read_bytes()))
    return result


@pytest.fixture
def source(tmp_path, monkeypatch, pairs):
    monkeypatch.setattr(certificates, "ROOT", tmp_path / "managed")
    certificate, key = tmp_path / "source.pem", tmp_path / "source.key"
    certificate.write_bytes(pairs[0][0])
    key.write_bytes(pairs[0][1])
    return certificate, key


def imported(source):
    return certificates.import_pair(*source, ["site.example.test"])


def test_external_certificate_is_not_automatically_renewed(source):
    result = imported(source)
    server = {'certificate_path':result['certificate_path'],'site_domain':'site.example.test','subscription_domain':'sub.example.test'}
    assert certificates.managed_acme(server) is False


def revisions():
    directory = certificates.ROOT / "certificates"
    return list(directory.iterdir()) if directory.exists() else []


def test_malformed_later_pem_cannot_overwrite_published_certificate(source):
    first = imported(source)
    published = Path(first["certificate_path"])
    original = published.read_bytes()
    source[0].write_bytes(original + b"-----BEGIN CERTIFICATE-----\nnot-valid-base64\n-----END CERTIFICATE-----\n")
    with pytest.raises(ValueError):
        imported(source)
    assert published.read_bytes() == original
    assert revisions() == [published.parent]


def test_changed_fullchain_gets_new_revision_and_identical_import_reuses_it(source, pairs):
    first = imported(source)
    previous = Path(first["certificate_path"])
    before = previous.stat()
    assert imported(source) == first
    after = previous.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    source[0].write_bytes(pairs[0][0] + pairs[1][0])
    second = imported(source)
    assert first["sha256"] == second["sha256"]
    assert first["certificate_path"] != second["certificate_path"]
    assert previous.read_bytes() == pairs[0][0]
    assert Path(second["certificate_path"]).read_bytes() == pairs[0][0] + pairs[1][0]


def test_source_change_after_validation_does_not_change_imported_bytes(source, pairs, monkeypatch):
    validate = certificates.validate_certificate
    def change_source(*args, **kwargs):
        result = validate(*args, **kwargs)
        source[0].write_bytes(pairs[1][0])
        source[1].write_bytes(pairs[1][1])
        return result
    monkeypatch.setattr(certificates, "validate_certificate", change_source)
    result = imported(source)
    assert Path(result["certificate_path"]).read_bytes() == pairs[0][0]
    assert Path(result["key_path"]).read_bytes() == pairs[0][1]


def test_mismatched_key_and_failed_staging_leave_no_revision(source, pairs, monkeypatch):
    source[1].write_bytes(pairs[1][1])
    with pytest.raises(ValueError):
        imported(source)
    assert revisions() == []
    source[1].write_bytes(pairs[0][1])
    write = certificates.atomic_write
    def fail_key(path, *args, **kwargs):
        if path.name == "privkey.pem":
            raise OSError("injected staging failure")
        return write(path, *args, **kwargs)
    monkeypatch.setattr(certificates, "atomic_write", fail_key)
    with pytest.raises(OSError):
        imported(source)
    assert revisions() == []


def test_existing_revision_tampering_is_rejected_without_repairing_it(source):
    first = imported(source)
    key = Path(first["key_path"])
    key.write_bytes(b"unexpected existing content")
    with pytest.raises(OperationError):
        imported(source)
    assert key.read_bytes() == b"unexpected existing content"


def test_concurrent_identical_imports_publish_one_complete_revision(source, pairs):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: imported(source), range(3)))
    assert all(result == results[0] for result in results)
    certificate, key = Path(results[0]["certificate_path"]), Path(results[0]["key_path"])
    assert revisions() == [certificate.parent]
    assert certificate.read_bytes() == pairs[0][0]
    assert key.read_bytes() == pairs[0][1]


@pytest.mark.parametrize("which,suffix", [
    (0, b"unexpected trailing text"),
    (0, b"-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----\n"),
    (1, b"unexpected trailing text"),
])
def test_trailing_text_and_invalid_der_are_rejected(source, which, suffix):
    source[which].write_bytes(source[which].read_bytes() + suffix)
    with pytest.raises(ValueError):
        imported(source)
    assert revisions() == []


def test_acme_source_uses_stable_binding_identity_and_validated_domains(tmp_path):
    marker = tmp_path/'acme.json'
    identity = 'a' * 32
    binding = {'id': identity, 'domains': ['site.example.test'],
               'certificate_path': str(tmp_path/'fullchain.pem')}
    source = {'source': 'acme-http-01', 'lineage': 'proxyhub-' + identity,
              'domains': ['site.example.test']}
    marker.write_text(json.dumps(source), encoding='utf-8')
    assert certificates.acme_source(binding) == source
    assert certificates.acme_source({**binding, 'id': 'b' * 32}) is None
    assert certificates.acme_source({**binding, 'domains': ['other.example.test']}) is None
    for domains in ([], 'site.example.test', ['site.example.test'] * 2, ['../unsafe']):
        marker.write_text(json.dumps({**source, 'domains': domains}), encoding='utf-8')
        assert certificates.acme_source(binding) is None


def test_legacy_acme_marker_remains_compatible_and_cannot_own_other_bindings(tmp_path):
    marker = tmp_path/'acme.json'
    source = {'source': 'acme-http-01', 'lineage': 'proxyhub',
              'domains': ['site.example.test', 'sub.example.test']}
    marker.write_text(json.dumps(source), encoding='utf-8')
    server = {'certificate_path': str(tmp_path/'fullchain.pem'),
              'site_domain': 'site.example.test', 'subscription_domain': 'sub.example.test'}
    assert certificates.managed_acme(server) is True
    binding = {'id': 'a' * 32, 'domains': source['domains'], 'certificate_path': server['certificate_path']}
    assert certificates.acme_source(binding) is None
    marker.write_text('invalid JSON', encoding='utf-8')
    assert certificates.managed_acme(server) is False


@pytest.mark.parametrize('lineage', ['../proxyhub', 'proxyhub-abc', 'proxyhub-' + 'A' * 32, 'arbitrary', None])
def test_issue_rejects_unmanaged_lineage_before_invoking_certbot(tmp_path, monkeypatch, lineage):
    monkeypatch.setattr(certificates, 'ROOT', tmp_path)
    monkeypatch.setattr(certificates, 'run', lambda *args, **kwargs: pytest.fail('Invalid lineage must not invoke Certbot'))
    with pytest.raises(OperationError, match='lineage'):
        certificates.issue(['site.example.test'], standalone=False, lineage=lineage)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('lineage', ['proxyhub', 'proxyhub-' + 'a' * 32])
def test_issue_preserves_lineage_in_certbot_paths_and_ownership(tmp_path, monkeypatch, lineage):
    monkeypatch.setattr(certificates, 'ROOT', tmp_path)
    arguments, imports = [], []
    monkeypatch.setattr(certificates, 'run', lambda args, **kwargs: arguments.append(args))
    published = tmp_path/'certificates'/'pair'
    published.mkdir(parents=True)
    def import_pair(cert, key, domains):
        imports.append((cert, key, domains))
        return {'certificate_path': str(published/'fullchain.pem'), 'key_path': str(published/'privkey.pem')}
    monkeypatch.setattr(certificates, 'import_pair', import_pair)
    result = certificates.issue(['site.example.test'], standalone=False, lineage=lineage)
    command = arguments[0]
    assert command[command.index('--cert-name') + 1] == lineage
    assert imports == [(tmp_path/'acme/config/live'/lineage/'fullchain.pem',
                        tmp_path/'acme/config/live'/lineage/'privkey.pem', ['site.example.test'])]
    identity = 'default' if lineage == 'proxyhub' else 'a' * 32
    assert certificates.acme_source({'id': identity, 'domains': ['site.example.test'],
                                     'certificate_path': result['certificate_path']}) == {
        'source': 'acme-http-01', 'lineage': lineage, 'domains': ['site.example.test']}


def test_identical_pair_cannot_be_taken_over_by_another_acme_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(certificates, 'ROOT', tmp_path)
    monkeypatch.setattr(certificates, 'run', lambda *args, **kwargs: None)
    published = tmp_path/'certificates'/'pair'
    published.mkdir(parents=True)
    marker = published/'acme.json'
    original = json.dumps({'source': 'acme-http-01', 'lineage': 'proxyhub',
                           'domains': ['site.example.test']})
    marker.write_text(original, encoding='utf-8')
    monkeypatch.setattr(certificates, 'import_pair', lambda *args: {
        'certificate_path': str(published/'fullchain.pem'), 'key_path': str(published/'privkey.pem')})
    with pytest.raises(OperationError, match='another ACME lineage'):
        certificates.issue(['site.example.test'], standalone=False, lineage='proxyhub-' + 'a' * 32)
    assert marker.read_text(encoding='utf-8') == original


def test_symlink_acme_marker_never_establishes_renewal_ownership(tmp_path):
    target = tmp_path/'external.json'
    target.write_text(json.dumps({'source': 'acme-http-01', 'lineage': 'proxyhub',
                                  'domains': ['site.example.test', 'sub.example.test']}), encoding='utf-8')
    try:
        (tmp_path/'acme.json').symlink_to(target)
    except OSError:
        pytest.skip('Creating symlinks requires OS privileges')
    server = {'certificate_path': str(tmp_path/'fullchain.pem'),
              'site_domain': 'site.example.test', 'subscription_domain': 'sub.example.test'}
    assert certificates.managed_acme(server) is False
