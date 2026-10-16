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
    assert output["proxies"] == config["proxies"]
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


def managed_node(protocol, **options):
    """A public synthetic snapshot, independent of a running VPS or secret state."""
    credentials = {'uuid': '1a234567-1234-4234-8234-123456789abc'}
    if protocol in ('ss', 'trojan', 'anytls', 'socks5'):
        credentials = {'password': 'synthetic-password-' + 'a' * 24}
    if protocol == 'socks5':
        credentials['username'] = 'synthetic-user'
        defaults = {'auth': True, 'udp': True}
    elif protocol == 'ss':
        defaults = {'method': 'aes-128-gcm'}
    else:
        defaults = {'tls': True, 'tls_mode': 'certificate', 'sni': 'tls.example.test', 'certificate_id': 'default'}
        if protocol in ('vless', 'vmess', 'trojan'):
            defaults['transport'] = 'tcp'
        if protocol == 'vless':
            defaults['flow'] = ''
        elif protocol == 'vmess':
            defaults['security'] = 'auto'
        elif protocol == 'tuic':
            defaults.update(congestion_control='bbr', udp_relay_mode='native')
            credentials['password'] = 'synthetic-password-' + 'a' * 24
    defaults.update(options)
    if defaults.get('tls') is False:
        for key in ('tls_mode', 'sni', 'certificate_id'):
            defaults.pop(key, None)
    if defaults.get('transport', 'tcp') != 'tcp':
        defaults.setdefault('host', 'transport.example.test')
        defaults.setdefault('path', '/synthetic')
    if protocol == 'ss' and defaults['method'].startswith('2022-'):
        length = 16 if defaults['method'] == '2022-blake3-aes-128-gcm' else 32
        credentials['password'] = base64.b64encode(bytes(range(length))).decode()
    if protocol == 'socks5' and not defaults['auth']:
        credentials = {}
    return {'id': 'b' * 32, 'protocol': protocol, 'name': '额外节点 & one',
            'server': '2001:db8::3', 'port': 15443, 'options': defaults, 'credentials': credentials}


def managed_matrix(protocol):
    if protocol == 'ss':
        return [managed_node(protocol, method=method) for method in (
            'aes-128-gcm', 'aes-256-gcm', 'chacha20-ietf-poly1305',
            '2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm', '2022-blake3-chacha20-poly1305')]
    if protocol == 'socks5':
        return [managed_node(protocol, auth=auth, udp=udp) for auth in (True, False) for udp in (True, False)]
    if protocol == 'tuic':
        return [managed_node(protocol, congestion_control=cc, udp_relay_mode=relay)
                for cc in ('bbr', 'cubic', 'new_reno') for relay in ('native', 'quic')]
    if protocol == 'anytls':
        return [managed_node(protocol)]
    nodes = [managed_node(protocol, transport=transport, tls=tls)
             for transport in ('tcp', 'ws', 'httpupgrade')
             for tls in ((True,) if protocol == 'trojan' else (False, True))]
    if protocol != 'trojan':
        nodes.append(managed_node(protocol, transport='h2'))
    if protocol == 'vmess':
        nodes.append(managed_node(protocol, transport='http', tls=False, security='none'))
        nodes.extend(managed_node(protocol, security=cipher)
                     for cipher in ('none', 'aes-128-gcm', 'chacha20-poly1305'))
    if protocol == 'vless':
        nodes.append(managed_node(protocol, flow='xtls-rprx-vision'))
        for transport in ('tcp', 'h2'):
            node = managed_node(protocol, transport=transport, tls_mode='reality')
            node['options'].pop('certificate_id')
            node['options']['reality_target_id'] = 'default'
            node['credentials'].update(public_key=base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='), short_id='aabbccdd')
            nodes.append(node)
    return nodes


@pytest.mark.parametrize('protocol', ['ss', 'socks5', 'tuic', 'anytls', 'trojan', 'vmess', 'vless'])
def test_managed_matrix_uses_single_snapshot_and_pinned_mihomo(input_document, tmp_path, protocol):
    nodes = managed_matrix(protocol)
    for index, node in enumerate(nodes):
        node.update(id=f'{index:032x}', name=f'额外节点 {protocol} {index}')
    input_document['nodes'] = nodes
    add_hy2(input_document)
    process = run_worker(input_document)
    assert process.returncode == 0 and process.stderr == ''
    output = json.loads(process.stdout)
    config = parse_yaml(output['yaml'])
    assert output['proxies'] == config['proxies']
    assert [node['name'] for node in config['proxies']] == [input_document['node']['name'], input_document['hy2']['name'], *[node['name'] for node in nodes]]
    assert config['proxy-groups'][0]['proxies'] == [node['name'] for node in config['proxies']]
    assert output['uri'] == output['uris'][0]
    assert base64.b64decode(output['raw']).decode().splitlines() == output['uris']
    for node, uri, proxy in zip(nodes, output['uris'][2:], output['proxies'][2:], strict=True):
        assert proxy['server'] == node['server'] and proxy['port'] == node['port']
        assert 'certificate_id' not in proxy and 'reality_target_id' not in proxy
        if protocol == 'vmess':
            data = json.loads(base64.b64decode(uri.removeprefix('vmess://')))
            assert data['add'] == node['server'] and data['id'] == node['credentials']['uuid']
            assert data['aid'] == '0' and data['scy'] == node['options']['security']
            assert (data['type'] == 'http') == (node['options']['transport'] == 'http')
        else:
            parsed = urlsplit(uri)
            assert parsed.hostname == node['server'] and parsed.port == node['port']
            assert unquote(parsed.fragment) == node['name']
        if protocol == 'ss':
            if node['options']['method'].startswith('2022-'):
                assert unquote(parsed.username) == node['options']['method']
                assert unquote(parsed.password) == node['credentials']['password']
            else:
                assert base64.urlsafe_b64decode(parsed.username + '=' * (-len(parsed.username) % 4)).decode() == node['options']['method'] + ':' + node['credentials']['password']
        if node['options'].get('transport') == 'httpupgrade':
            assert proxy['network'] == 'ws' and proxy['ws-opts']['v2ray-http-upgrade'] is True
        if node['options'].get('transport') == 'h2':
            assert proxy['udp'] is False
        if proxy['udp'] is False:
            assert config['rules'] == ['MATCH,PROXY', 'MATCH,REJECT']
        if protocol == 'tuic':
            assert unquote(parsed.username) == node['credentials']['uuid']
            assert unquote(parsed.password) == node['credentials']['password']
            assert parse_qs(parsed.query)['udp_relay_mode'] == [node['options']['udp_relay_mode']]
    configured = os.environ.get('MIHOMO_BIN')
    binary = Path(configured) if configured else ROOT / '.cache/runtime/windows-amd64/mihomo.exe'
    if not binary.is_file():
        pytest.skip('Optional pinned Mihomo binary is not installed; set MIHOMO_BIN')
    path = tmp_path / 'mihomo.yaml'
    path.write_text(output['yaml'], encoding='utf-8')
    checked = subprocess.run([str(binary), '-t', '-d', str(tmp_path), '-f', str(path)], capture_output=True, timeout=30)
    assert checked.returncode == 0, 'Pinned Mihomo rejected the managed protocol matrix'


@pytest.mark.parametrize('protocol', ['vless', 'vmess', 'socks5'])
def test_udp_limited_nodes_reject_rule_fallthrough_without_changing_shared_transport(input_document, protocol):
    options = {'udp': False} if protocol == 'socks5' else {'transport': 'h2'}
    input_document['nodes'] = [managed_node(protocol, **options)]
    process = run_worker(input_document)
    assert process.returncode == 0
    output = json.loads(process.stdout)
    config = parse_yaml(output['yaml'])
    assert output['proxies'] == config['proxies']
    assert config['proxies'][0]['udp'] is True
    assert config['proxies'][1]['udp'] is False
    assert config['rules'] == ['MATCH,PROXY', 'MATCH,REJECT']
    if protocol != 'socks5':
        assert config['proxies'][1]['network'] == 'h2'
    if protocol == 'vmess':
        uri = json.loads(base64.b64decode(output['uris'][1].removeprefix('vmess://')))
        assert uri['net'] == 'h2' and uri['tls'] == 'tls'
    elif protocol == 'vless':
        assert parse_qs(urlsplit(output['uris'][1]).query)['type'] == ['h2']
    else:
        assert parse_qs(urlsplit(output['uris'][1]).query)['udp'] == ['0']


@pytest.mark.parametrize('mutate', [
    lambda n: n.update(unknown='secret-input-marker'),
    lambda n: n['options'].update(unknown=True),
    lambda n: n['credentials'].update(password='secret-input-marker'),
    lambda n: n['options'].update(tls=False),
    lambda n: n['options'].update(transport='grpc'),
    lambda n: n['options'].update(transport='ws', host='transport.example.test', path='/x', flow='xtls-rprx-vision'),
    lambda n: n.update(name='PROXY'),
    lambda n: n.update(id='not-a-managed-id'),
    lambda n: n['options'].update(sni='127.0.0.1'),
    lambda n: n['options'].update(transport='ws', host='localhost', path='/x'),
    lambda n: n['options'].update(transport='ws', host='transport.example.test', path='/x?ed=1'),
])
def test_managed_invalid_or_incompatible_fields_fail_closed(input_document, mutate):
    node = managed_node('vless')
    mutate(node)
    input_document['nodes'] = [node]
    process = run_worker(input_document)
    assert process.returncode != 0 and process.stdout == ''
    assert process.stderr == 'converter: invalid or unsupported input\n'


def test_unsupported_protocol_combinations_and_duplicate_resources_are_rejected(input_document):
    rejected = [managed_node('trojan', transport='h2'),
                managed_node('vmess', transport='http', security='auto', tls=False),
                managed_node('vmess', transport='h2', tls=False)]
    reality_ws = managed_node('vless', transport='ws', tls_mode='reality')
    reality_ws['options'].pop('certificate_id')
    reality_ws['options']['reality_target_id'] = 'default'
    reality_ws['credentials'].update(public_key=input_document['node']['public_key'], short_id='aabb')
    rejected.append(reality_ws)
    for node in rejected:
        input_document['nodes'] = [node]
        assert run_worker(input_document).returncode != 0
    node = managed_node('anytls')
    for nodes in ([node, node], [dict(node, id=input_document['node']['id'])],
                  [dict(node, name=input_document['node']['name'])], None, [node] * 33):
        input_document['nodes'] = nodes
        assert run_worker(input_document).returncode != 0
