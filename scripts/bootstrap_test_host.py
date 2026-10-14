#!/usr/bin/env python3
"""Install pinned runtimes into /opt/proxyhub only. No distro service is replaced."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    if not args.install:
        parser.error('Pass --install after reviewing the pinned asset manifests')
    if os.name != 'posix' or os.geteuid() != 0 or platform.machine() != 'x86_64':
        parser.error('This bootstrap targets a dedicated Debian 12 amd64 test host as root')
    release = Path('/etc/os-release').read_text()
    if 'ID=debian' not in release or 'VERSION_ID="12"' not in release:
        parser.error('This bootstrap only handles Debian 12; other OS adapters are pending')
    prefix = Path('/opt/proxyhub')
    marker = prefix / '.proxyhub-owned'
    if prefix.exists() and not marker.exists():
        raise SystemExit('Existing /opt/proxyhub is not owned by this installer')
    prefix.mkdir(mode=0o755, exist_ok=True)
    marker.touch(mode=0o600, exist_ok=True)
    cache = prefix / 'downloads'
    cache.mkdir(mode=0o700, exist_ok=True)
    runtimes = prefix / 'runtimes'
    runtimes.mkdir(mode=0o755, exist_ok=True)
    bindir = prefix / 'bin'
    bindir.mkdir(mode=0o755, exist_ok=True)
    assets = json.loads((ROOT/'evidence/runtime-assets.json').read_text())['assets']
    assets = [a for a in assets if a['platform'] == 'linux-amd64']
    assets += json.loads((ROOT/'evidence/bootstrap-assets.json').read_text())['assets']
    results = []
    for item in assets:
        name = item['url'].rsplit('/', 1)[-1]
        archive = cache/name
        if not archive.exists():
            with urllib.request.urlopen(item['url'], timeout=60) as incoming, archive.open('wb') as outgoing:
                while data := incoming.read(1024*1024):
                    outgoing.write(data)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != item['sha256']:
            raise SystemExit('SHA256 mismatch: '+name)
        if name.startswith('sing-box-'):
            binary = runtimes/'sing-box-1.14.3'
            with tarfile.open(archive) as bundle:
                binary.write_bytes(bundle.extractfile('sing-box-1.14.3-linux-amd64/sing-box').read())
            alias = 'sing-box'
        elif name.startswith('mihomo-'):
            binary = runtimes/'mihomo-1.19.32'
            with gzip.open(archive) as data:
                binary.write_bytes(data.read())
            alias = 'mihomo'
        elif name.startswith('node-'):
            binary = runtimes/'node-24.21.0'
            with tarfile.open(archive) as bundle:
                binary.write_bytes(bundle.extractfile('node-v24.21.0-linux-x64/bin/node').read())
            alias = 'node'
        else:
            extracted = runtimes/'nginx-1.30.5'
            subprocess.run(['dpkg-deb','-x',str(archive),str(extracted)],check=True)
            binary = extracted/'usr/sbin/nginx'
            alias = 'nginx'
        binary.chmod(0o755)
        target = bindir/alias
        if target.exists() or target.is_symlink():
            if target.resolve() != binary.resolve():
                raise SystemExit('Runtime alias conflict: '+alias)
        else:
            target.symlink_to(binary)
        results.append({'name':alias,'sha256_verified':True,'version':item['version']})
    print(json.dumps(results))


if __name__ == '__main__':
    main()
