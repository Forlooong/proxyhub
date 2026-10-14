#!/usr/bin/env python3
"""Download pinned official assets and record executable, non-service checks.

No installation, PATH edits or service changes. Downloaded files stay under
.cache/runtime. Only the native platform is executed. Linux ARM64 must be
checked on a matching runner before claiming production support.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache' / 'runtime'
EVIDENCE = ROOT / 'evidence' / 'runtime-verification.json'


def digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def fetch(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.part')
    request = urllib.request.Request(url, headers={'User-Agent': 'ProxyHub-phase0/0.1'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open('wb') as target:
                shutil.copyfileobj(response, target)
            temporary.replace(destination)
            return
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def asset_check(asset: dict) -> dict:
    result = dict(asset)
    destination = CACHE / asset['name']
    try:
        if not destination.exists() or digest(destination) != asset['sha256']:
            fetch(asset['url'], destination)
        actual = digest(destination)
        result.update(downloaded=True, observed_sha256=actual, observed_size=destination.stat().st_size)
        result['status'] = 'passed' if actual == asset['sha256'] and destination.stat().st_size == asset['size'] else 'failed'
        if result['status'] != 'passed':
            result['error'] = 'Official digest or asset size mismatch; binary will not execute.'
    except Exception as error:
        result.update(status='blocked', downloaded=False, error=str(error))
    return result


def executable(asset: dict) -> Path:
    archive = CACHE / asset['name']
    suffix = '.exe' if asset['platform'].startswith('windows') else ''
    output = CACHE / asset['platform'] / (asset['project'] + suffix)
    output.parent.mkdir(parents=True, exist_ok=True)
    wanted = asset['project'] + suffix
    # Copy exactly one known executable; never extract archive paths wholesale.
    if archive.suffix == '.zip':
        with zipfile.ZipFile(archive) as source:
            candidates = [item for item in source.infolist() if not item.is_dir() and (Path(item.filename).name == wanted or (asset['project'] == 'mihomo' and item.filename.endswith('.exe')))]
            if len(candidates) != 1:
                raise ValueError('Expected exactly one executable in ZIP')
            with source.open(candidates[0]) as incoming, output.open('wb') as target:
                shutil.copyfileobj(incoming, target)
    elif archive.name.endswith('.tar.gz'):
        with tarfile.open(archive, 'r:gz') as source:
            candidates = [item for item in source.getmembers() if item.isfile() and Path(item.name).name == wanted]
            if len(candidates) != 1:
                raise ValueError('Expected exactly one executable in tar archive')
            incoming = source.extractfile(candidates[0])
            if incoming is None:
                raise ValueError('Missing archive data')
            with incoming, output.open('wb') as target:
                shutil.copyfileobj(incoming, target)
    else:
        with gzip.open(archive, 'rb') as incoming, output.open('wb') as target:
            shutil.copyfileobj(incoming, target)
    if os.name != 'nt':
        output.chmod(0o700)
    return output


def run(binary: Path, args: list[str], expected_code: int = 0, contains: str | None = None) -> dict:
    try:
        completed = subprocess.run([str(binary), *args], cwd=CACHE, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        # Local absolute paths are irrelevant to public reproducibility evidence.
        output = (completed.stdout + completed.stderr).replace(str(ROOT), '<project>').replace(str(ROOT).replace('\\', '/'), '<project>')
        passed = completed.returncode == expected_code and (contains is None or contains.lower() in output.lower())
        return {'status': 'passed' if passed else 'failed', 'returncode': completed.returncode, 'output': output.strip()}
    except Exception as error:
        return {'status': 'failed', 'error': str(error)}


def native_checks(assets: list[dict]) -> list[dict]:
    arch = {'AMD64': 'amd64', 'x86_64': 'amd64', 'aarch64': 'arm64', 'ARM64': 'arm64'}.get(platform.machine(), platform.machine())
    native = ('windows' if os.name == 'nt' else platform.system().lower()) + '-' + arch
    checks = []
    for asset in assets:
        if asset['platform'] != native:
            checks.append({'project': asset['project'], 'platform': asset['platform'], 'status': 'pending', 'reason': 'No matching native execution environment in this run.'})
            continue
        if asset['status'] != 'passed':
            checks.append({'project': asset['project'], 'platform': native, 'status': 'blocked', 'reason': 'Asset has not passed digest verification.'})
            continue
        binary = executable(asset)
        version = run(binary, ['version'] if asset['project'] == 'sing-box' else ['-v'], contains=asset['version'])
        checks.append({'project': asset['project'], 'platform': native, 'test': 'version', **version})
        if version['status'] != 'passed':
            continue
        if asset['project'] == 'sing-box':
            key_output = subprocess.run([str(binary), 'generate', 'reality-keypair'], capture_output=True, text=True, check=True, timeout=10).stdout
            private_key = re.search(r'PrivateKey:\s*(\S+)', key_output).group(1)
            config = {'log': {'disabled': True}, 'inbounds': [{'type': 'vless', 'tag': 'reality-test', 'listen': '127.0.0.1', 'listen_port': 12443, 'users': [{'name': 'test', 'uuid': '00000000-0000-4000-8000-000000000001', 'flow': 'xtls-rprx-vision'}], 'tls': {'enabled': True, 'server_name': 'site.example.test', 'reality': {'enabled': True, 'handshake': {'server': '127.0.0.1', 'server_port': 18443}, 'private_key': private_key, 'short_id': ['0123456789abcdef']}}}], 'outbounds': [{'type': 'direct', 'tag': 'direct'}]}
            cfg_path = CACHE / 'sing-box-capability.json'
            try:
                cfg_path.write_text(json.dumps(config), encoding='utf-8')
                checks.append({'project': asset['project'], 'platform': native, 'test': 'reality-local-target-config', **run(binary, ['check', '-c', str(cfg_path)])})
                config['experimental'] = {'v2ray_api': {'listen': '127.0.0.1:10085', 'stats': {'enabled': True, 'inbounds': ['reality-test']}}}
                cfg_path.write_text(json.dumps(config), encoding='utf-8')
                result = run(binary, ['check', '-c', str(cfg_path)], expected_code=1, contains='with_v2ray_api')
                checks.append({'project': asset['project'], 'platform': native, 'test': 'default-v2ray-api-rejected', 'expected': 'Build without with_v2ray_api must reject the stats configuration.', **result})
            finally:
                cfg_path.unlink(missing_ok=True)
        else:
            cfg_path = CACHE / 'mihomo-capability.json'
            config = {'mode': 'rule', 'log-level': 'silent', 'proxies': [], 'proxy-groups': [{'name': 'PROXY', 'type': 'select', 'proxies': ['DIRECT']}], 'rules': ['MATCH,PROXY'], 'dns': {'enable': False}}
            try:
                cfg_path.write_text(json.dumps(config), encoding='utf-8')
                checks.append({'project': asset['project'], 'platform': native, 'test': 'minimal-subscription-config', **run(binary, ['-t', '-d', str(CACHE / 'mihomo-home'), '-f', str(cfg_path)])})
            finally:
                cfg_path.unlink(missing_ok=True)
    return checks


def nginx_check() -> dict:
    url = 'https://nginx.org/download/nginx-1.30.5.tar.gz'
    destination = CACHE / 'nginx-1.30.5.tar.gz'
    try:
        if not destination.exists():
            fetch(url, destination)
        with tarfile.open(destination, 'r:gz') as archive:
            member = archive.extractfile('nginx-1.30.5/src/core/nginx.h')
            if member is None:
                raise ValueError('Missing nginx version header')
            version_header = member.read().decode()
        if '"1.30.5"' not in version_header:
            raise ValueError('Source version mismatch')
        result = {'version': '1.30.5', 'status': 'source-downloaded', 'url': url, 'observed_sha256': digest(destination), 'observed_size': destination.stat().st_size, 'version_header_matched': True, 'signature_verification': 'pending', 'execution': 'pending-linux-build', 'required_build_flags': ['--with-stream', '--with-stream_ssl_preread_module', '--with-http_ssl_module']}
        gpg = shutil.which('gpg')
        if gpg:
            try:
                key_home = CACHE / 'nginx-keyring'
                key_home.mkdir(mode=0o700, exist_ok=True)
                key_urls = ['https://nginx.org/keys/' + name + '.key' for name in ['arut', 'pluknet', 'sb', 'thresh']]
                # Relative POSIX paths also work with Git for Windows' MSYS GPG.
                base = [gpg, '--no-options', '--homedir', key_home.relative_to(ROOT).as_posix(), '--batch', '--no-auto-key-retrieve']
                for key_url in key_urls:
                    key_path = CACHE / key_url.rsplit('/', 1)[-1]
                    if not key_path.exists():
                        fetch(key_url, key_path)
                    subprocess.run([*base, '--import', key_path.relative_to(ROOT).as_posix()], cwd=ROOT, capture_output=True, check=True, timeout=15)
                signature = destination.with_suffix(destination.suffix + '.asc')
                fetch(url + '.asc', signature)
                verify = subprocess.run([*base, '--status-fd', '1', '--verify', signature.relative_to(ROOT).as_posix(), destination.relative_to(ROOT).as_posix()], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
                match = re.search(r'\[GNUPG:\] VALIDSIG ([0-9A-F]+)', verify.stdout)
                result['signature_verification'] = 'passed' if verify.returncode == 0 and match else 'failed'
                result['signature_checked_at'] = datetime.now(timezone.utc).isoformat()
                result['signer_fingerprint'] = match.group(1) if match else None
                result['signature_key_sources'] = key_urls
                result['signature_source'] = url + '.asc'
                result['signature_trust_scope'] = 'Detached signature verified against keys obtained from the official nginx HTTPS key page; no independent key-signing trust chain established.'
            except Exception as error:
                result['signature_verification'] = 'blocked'
                result['signature_error'] = str(error)
        return result
    except Exception as error:
        return {'version': '1.30.5', 'status': 'blocked', 'url': url, 'error': str(error)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata-only', action='store_true', help='Do not execute downloaded programs')
    args = parser.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / 'evidence' / 'runtime-assets.json').read_text(encoding='utf-8'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assets = list(pool.map(asset_check, manifest['assets']))
    result = {'schema_version': 1, 'verified_at': datetime.now(timezone.utc).isoformat(), 'host_platform': platform.system() + '-' + platform.machine(), 'python_version': platform.python_version(), 'official_metadata_checked_at': manifest['checked_at'], 'assets': assets, 'checks': [] if args.metadata_only else native_checks(assets), 'nginx': nginx_check(), 'limits': ['Configuration checks do not establish real proxy connectivity.', 'Clash Verge Rev GUI import/display remains a separate locked-version acceptance test.', 'Public DNS, ACME HTTP-01 and VPS Linux SSH/systemd behavior require a deployment acceptance environment.', 'Nginx source download/hash alone does not verify publisher signature or runtime modules.']}
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'assets': [{k: a[k] for k in ('project', 'platform', 'status')} for a in assets], 'checks': [{k: c[k] for k in ('project', 'platform', 'test', 'status') if k in c} for c in result['checks']], 'nginx': result['nginx']['status'], 'evidence': str(EVIDENCE.relative_to(ROOT))}, indent=2))
    failed = any(a['status'] != 'passed' for a in assets) or any(c['status'] == 'failed' for c in result['checks'])
    failed = failed or result['nginx']['status'] == 'blocked' or result['nginx'].get('signature_verification') in {'blocked', 'failed'}
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
