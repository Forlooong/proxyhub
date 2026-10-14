"""Opt-in real public website/subscription/REALITY test, with private local inputs.

No system proxy is changed. The temporary Mihomo process is always stopped.
The output contains no host, token, key, UUID or subscription body.
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import time

import requests
import yaml

parser = argparse.ArgumentParser()
parser.add_argument('--config', type=Path, required=True)
parser.add_argument('--mihomo', type=Path, required=True)
parser.add_argument('--work', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
config = json.loads(args.config.read_text(encoding='utf-8'))
server = config['server']
profile = config['subscription']
args.work.mkdir(parents=True, exist_ok=True)
session = requests.Session()
session.trust_env = False
port = server['public_port']
suffix = '' if port == 443 else ':'+str(port)
site = 'https://'+server['site_domain']+suffix
base = 'https://'+server['subscription_domain']+suffix
url = base+'/sub/'+profile['token']+'/mihomo.yaml'
report = {'schema_version':1, 'website_https':False, 'subscription_get_head':False,
          'unknown_token_rejected':False, 'mihomo_config_import':False,
          'reality_real_proxy_request':False, 'client_overrides_preserved':False, 'gui_import':'pending'}
process = None
try:
    response = session.get(site, timeout=25)
    response.raise_for_status()
    assert 'ProxyHub managed website' in response.text
    report['website_https'] = True
    response = session.get(url, timeout=25)
    response.raise_for_status()
    head = session.head(url, timeout=25)
    assert head.status_code == 200
    for name in ('Content-Disposition','Profile-Update-Interval','Subscription-Userinfo','ETag'):
        assert head.headers.get(name) == response.headers.get(name)
    assert response.headers['Profile-Update-Interval'] == str(profile['update_interval_hours'])
    if profile['userinfo']:
        for key,value in profile['userinfo'].items():
            assert f'{key}={value}' in response.headers['Subscription-Userinfo']
    else:
        assert 'Subscription-Userinfo' not in response.headers
    report['subscription_get_head'] = True
    parsed = yaml.safe_load(response.text)
    for key,value in config['overrides'].items():
        assert parsed[key] == value
    report['client_overrides_preserved'] = True
    invalid = session.get(base+'/sub/'+'invalid-token-'*4+'/mihomo.yaml', timeout=25)
    assert invalid.status_code == 404
    report['unknown_token_rejected'] = True
    client = args.work/'client.yaml'
    client.write_bytes(response.content)
    executable = str(args.mihomo.resolve())
    result = subprocess.run([executable,'-t','-d',str(args.work.resolve()),'-f',str(client.resolve())], capture_output=True)
    assert result.returncode == 0
    report['mihomo_config_import'] = True
    mixed_port = config['overrides'].get('mixed-port',17890)
    with socket.socket() as available:
        available.bind(('127.0.0.1', mixed_port))
    with (args.work/'client.log').open('wb') as log:
        process = subprocess.Popen([executable,'-d',str(args.work.resolve()),'-f',str(client.resolve())],stdout=log,stderr=log)
        for _ in range(40):
            assert process.poll() is None
            try:
                with socket.create_connection(('127.0.0.1',mixed_port),timeout=0.2):
                    break
            except OSError:
                time.sleep(0.25)
        proxy = 'http://127.0.0.1:'+str(mixed_port)
        result = session.get('https://api.ipify.org?format=json',proxies={'https':proxy,'http':proxy},timeout=45)
        result.raise_for_status()
        assert result.json()['ip'] == server['public_address']
        report['reality_real_proxy_request'] = True
finally:
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))
