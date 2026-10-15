"""Exercise the real Node worker and, when installed, the pinned Mihomo binary."""

import base64
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "packages/converter/dist/worker.js"
NODE = shutil.which("node")


@pytest.fixture(scope="module", autouse=True)
def build_worker():
    if NODE is None:
        pytest.skip("Node 24 is required for converter integration tests")
    compiler = ROOT / "node_modules/typescript/bin/tsc"
    assert compiler.exists(), "Run npm ci before converter integration tests"
    subprocess.run([NODE, str(compiler), "-p", str(ROOT / "tsconfig.json")], cwd=ROOT, check=True, capture_output=True, timeout=30)


@pytest.fixture
def input_document():
    return {
        "node": {
            "id": "node_1", "name": "测试节点 & one", "server": "proxy.example.test", "port": 443,
            "uuid": "1a234567-1234-4234-8234-123456789abc", "server_name": "www.example.test",
            "public_key": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
            "short_id": "aabbccdd",
        },
        "overrides": {},
    }


def run_worker(document):
    return subprocess.run([NODE, str(WORKER)], input=json.dumps(document), capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=10)


def parse_yaml(text):
    process = subprocess.run(
        [NODE, "--input-type=module", "-e", "import {parse} from 'yaml'; let s=''; for await (const x of process.stdin) s+=x; process.stdout.write(JSON.stringify(parse(s)));"],
        input=text, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=10, check=True,
    )
    return json.loads(process.stdout)


def test_config_and_uri_share_node_snapshot(input_document):
    process = run_worker(input_document)
    assert process.returncode == 0 and process.stderr == ""
    output = json.loads(process.stdout)
    config = parse_yaml(output["yaml"])
    assert config["mixed-port"] == 17890
    assert config["allow-lan"] is False and config["bind-address"] == "127.0.0.1"
    assert config["tun"]["enable"] is False
    assert config["proxy-groups"] == [{"name": "PROXY", "type": "select", "proxies": [input_document["node"]["name"]]}]
    assert config["rules"] == ["MATCH,PROXY"]
    assert config["dns"]["respect-rules"] is False
    assert config["dns"]["proxy-server-nameserver"] == ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"]
    assert config["proxies"][0]["uuid"] == input_document["node"]["uuid"]
    assert config["proxies"][0]["flow"] == "xtls-rprx-vision"
    uri = urlsplit(output["uri"])
    query = parse_qs(uri.query)
    assert uri.scheme == "vless" and uri.hostname == input_document["node"]["server"]
    assert unquote(uri.username) == input_document["node"]["uuid"]
    assert unquote(uri.fragment) == input_document["node"]["name"]
    assert query["pbk"] == [input_document["node"]["public_key"]]
    assert query["sni"] == [input_document["node"]["server_name"]]
    assert base64.b64decode(output["raw"]).decode() == output["uri"] + "\n"


def test_allowed_overrides_are_preserved(input_document):
    input_document["overrides"] = {"mixed-port": 18080, "mode": "global", "log-level": "info", "ipv6": True, "allow-lan": False}
    output = json.loads(run_worker(input_document).stdout)
    config = parse_yaml(output["yaml"])
    for key, value in input_document["overrides"].items():
        assert config[key] == value
    assert config["dns"]["ipv6"] is True


@pytest.mark.parametrize("overrides", [
    {"proxies": []}, {"proxy-groups": []}, {"rules": []}, {"dns": {}}, {"tun": {"enable": True}},
    {"unsupported": True}, {"mixed-port": 0}, {"mixed-port": True}, {"mixed-port": "17890"},
    {"allow-lan": True}, {"mode": "not-a-mode"}, {"log-level": None}, {"ipv6": None},
])
def test_unknown_protected_and_invalid_overrides_fail_closed(input_document, overrides):
    input_document["overrides"] = overrides
    process = run_worker(input_document)
    assert process.returncode != 0 and process.stdout == ""
    assert input_document["node"]["uuid"] not in process.stderr
    assert process.stderr == "converter: invalid or unsupported input\n"


@pytest.mark.parametrize(("field", "value"), [
    ("password", "secret-input-marker"), ("uuid", "secret-input-marker"),
    ("public_key", "invalid"), ("short_id", "a"), ("server", "https://bad.test"),
    ("server_name", "bad.test\r\ninjection"), ("name", "PROXY"),
    ("name", "PASS"), ("name", "REJECT-DROP"), ("name", "COMPATIBLE"),
])
def test_invalid_and_unknown_node_fields_are_not_silently_dropped(input_document, field, value):
    input_document["node"][field] = value
    process = run_worker(input_document)
    assert process.returncode != 0 and process.stdout == ""
    assert "secret-input-marker" not in process.stderr


def test_ipv6_uri_has_bracketed_authority(input_document):
    input_document["node"]["server"] = "2001:db8::1"
    output = json.loads(run_worker(input_document).stdout)
    assert "@[2001:db8::1]:443?" in output["uri"]
    assert parse_yaml(output["yaml"])["proxies"][0]["server"] == "2001:db8::1"


def add_hy2(document):
    document['hy2'] = {
        'id': 'local-hy2', 'name': '测试 HY2 & two', 'server': '2001:db8::2', 'port': 443,
        'password': 'test-only-hy2-password-' + 'a' * 21, 'server_name': 'hy2.example.test'}
    return document


def test_hy2_and_reality_share_snapshot_with_reality_selected_first(input_document):
    output = json.loads(run_worker(add_hy2(input_document)).stdout)
    config = parse_yaml(output['yaml'])
    node = input_document['hy2']
    assert config['proxies'][1] == {
        'name': node['name'], 'type': 'hysteria2', 'server': node['server'], 'port': 443,
        'password': node['password'], 'sni': node['server_name'], 'skip-cert-verify': False}
    assert config['proxy-groups'][0]['proxies'] == [input_document['node']['name'], node['name']]
    assert output['uris'][0] == output['uri']
    assert base64.b64decode(output['raw']).decode().splitlines() == output['uris']
    uri = urlsplit(output['uris'][1])
    assert uri.scheme == 'hysteria2' and uri.hostname == node['server'] and uri.port == 443
    assert unquote(uri.username) == node['password'] and unquote(uri.fragment) == node['name']
    assert parse_qs(uri.query) == {'sni': [node['server_name']]}


@pytest.mark.parametrize(('field', 'value'), [
    ('id', 'other'), ('name', 'PROXY'), ('name', '测试节点 & one'), ('port', True),
    ('password', 'secret-input-marker'), ('password', 'a' * 31 + ':'),
    ('server_name', 'bad.test\r\ninjection'), ('server_name', '127.0.0.1'),
    ('server_name', 'localhost'), ('skip-cert-verify', True), ('obfs', 'salamander'),
])
def test_invalid_hy2_fields_fail_without_echoing_credentials(input_document, field, value):
    add_hy2(input_document)['hy2'][field] = value
    process = run_worker(input_document)
    assert process.returncode != 0 and process.stdout == ''
    assert process.stderr == 'converter: invalid or unsupported input\n'


def test_hy2_requires_an_object_and_all_credential_fields(input_document):
    for value in (None, {}, {'id': 'local-hy2'}):
        input_document['hy2'] = value
        process = run_worker(input_document)
        assert process.returncode != 0 and process.stdout == ''


def test_input_size_limit():
    process = subprocess.run([NODE, str(WORKER)], input=" " * (64 * 1024 + 1), capture_output=True, text=True, cwd=ROOT, timeout=10)
    assert process.returncode != 0 and process.stdout == ""
    assert process.stderr == "converter: invalid or unsupported input\n"


@pytest.mark.parametrize("text", ['{"node":{},"node":{}}', '{"node":{},"\\u006eode":{}}', '{not json}'])
def test_malformed_or_duplicate_json_is_rejected(text):
    process = subprocess.run([NODE, str(WORKER)], input=text, capture_output=True, text=True, cwd=ROOT, timeout=10)
    assert process.returncode != 0 and process.stdout == ""
    assert process.stderr == "converter: invalid or unsupported input\n"


@pytest.mark.parametrize('with_hy2', [False, True])
def test_pinned_mihomo_accepts_generated_config(input_document, tmp_path, with_hy2):
    configured = os.environ.get("MIHOMO_BIN")
    binary = Path(configured) if configured else ROOT / ".cache/runtime/windows-amd64/mihomo.exe"
    if not binary.is_file():
        pytest.skip("Optional pinned Mihomo binary is not installed; set MIHOMO_BIN")
    document = copy.deepcopy(input_document)
    if with_hy2:
        add_hy2(document)
    output = json.loads(run_worker(document).stdout)
    config = tmp_path / "mihomo.yaml"
    config.write_text(output["yaml"], encoding="utf-8")
    process = subprocess.run([str(binary), "-t", "-d", str(tmp_path), "-f", str(config)], capture_output=True, timeout=30)
    assert process.returncode == 0, "Pinned Mihomo rejected the generated fixture config"
