"""Install only explicitly named ProxyHub users, units and entrypoint."""
from __future__ import annotations

import os
from pathlib import Path

from .core import OperationError, atomic_write, run

PREFIX = Path('/opt/proxyhub')
DATA = Path('/var/lib/proxyhub')
UNITS = ('proxyhub-proxy.service', 'proxyhub-publisher.service', 'proxyhub-nginx.service')


def units() -> dict[str, str]:
    common = '''Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=yes
'''
    header = '# Managed by ProxyHub\n[Unit]\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\n'
    return {
        'proxyhub-proxy.service': header + 'User=proxyhub-proxy\nGroup=proxyhub-proxy\n'
            'ExecStart=/opt/proxyhub/bin/sing-box run -c /var/lib/proxyhub/current/sing-box.json\n'
            + common.replace('AF_UNIX AF_INET AF_INET6', 'AF_UNIX AF_INET AF_INET6 AF_NETLINK') + '\n[Install]\nWantedBy=multi-user.target\n',
        'proxyhub-reality@.service': header + 'User=proxyhub-proxy\nGroup=proxyhub-proxy\n'
            'ExecStart=/opt/proxyhub/bin/sing-box run -c /var/lib/proxyhub/current/reality/%i.json\n'
            + common.replace('AF_UNIX AF_INET AF_INET6', 'AF_UNIX AF_INET AF_INET6 AF_NETLINK') + '\n[Install]\nWantedBy=multi-user.target\n',
        'proxyhub-publisher.service': header + 'User=proxyhub-pub\nGroup=proxyhub-pub\n'
            'Environment=PYTHONPATH=/opt/proxyhub/app/src\n'
            'ExecStart=/usr/bin/python3 -m proxyhub.publisher --bundle /var/lib/proxyhub/publish/bundle.json --listen 127.0.0.1 --port 18890\n'
            'ExecReload=/bin/kill -HUP $MAINPID\n' + common + '\n[Install]\nWantedBy=multi-user.target\n',
        'proxyhub-nginx.service': header + 'RuntimeDirectory=proxyhub\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes\n'
            'ExecStart=/opt/proxyhub/bin/nginx -e stderr -p /var/lib/proxyhub/nginx/ -c /var/lib/proxyhub/current/nginx.conf -g "daemon off;"\n'
            'ExecReload=/bin/kill -HUP $MAINPID\n'
            'KillSignal=SIGQUIT\nTimeoutStopSec=10\n'
            'ReadWritePaths=/var/lib/proxyhub/nginx /run/proxyhub\n'
            'CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_SETUID CAP_SETGID CAP_DAC_OVERRIDE CAP_CHOWN\n'
            + common + '\n[Install]\nWantedBy=multi-user.target\n',
        'proxyhub-renew.service': '# Managed by ProxyHub\n[Unit]\n'
            'Description=Renew ProxyHub certificates and activate validated consumers\n'
            'After=network-online.target\nWants=network-online.target\n'
            'ConditionPathExists=/var/lib/proxyhub/current/config.json\n\n[Service]\n'
            'Type=oneshot\nExecStart=/usr/local/bin/proxyhub deployment renew\n'
            'TimeoutStartSec=600\nUMask=0077\n',
        'proxyhub-renew.timer': '# Managed by ProxyHub\n[Unit]\n'
            'Description=Daily ProxyHub certificate renewal check\n\n[Timer]\n'
            'OnCalendar=*-*-* 04:20:00\nRandomizedDelaySec=1h\nPersistent=true\n'
            'Unit=proxyhub-renew.service\n\n[Install]\nWantedBy=timers.target\n',
    }


def install() -> dict:
    import pwd
    if PREFIX.is_symlink() or (PREFIX/'.proxyhub-owned').is_symlink() or not (PREFIX/'.proxyhub-owned').is_file():
        raise OperationError('Install the pinned runtimes first')
    for name, content in units().items():
        target = Path('/etc/systemd/system')/name
        if target.exists() and not target.read_text().startswith('# Managed by ProxyHub\n'):
            raise OperationError('Existing service unit is not owned by ProxyHub')
    launcher = Path('/usr/local/bin/proxyhub')
    if launcher.exists() and '# Managed by ProxyHub' not in launcher.read_text():
        raise OperationError('Existing proxyhub command is not owned by this installer')
    users = ['proxyhub-web', 'proxyhub-proxy', 'proxyhub-pub']
    for user in users:
        try:
            entry = pwd.getpwnam(user)
            if entry.pw_shell != '/usr/sbin/nologin' or entry.pw_dir != '/nonexistent':
                raise OperationError('Existing service account does not match the required identity')
        except KeyError:
            run(['useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', user])
    if DATA.is_symlink():
        raise OperationError('Data directory must not be a symlink')
    DATA.mkdir(mode=0o711, exist_ok=True)
    DATA.chmod(0o711)
    for name in ('revisions', 'plans', 'certificates', 'nginx', 'acme', 'publish'):
        if (DATA/name).is_symlink():
            raise OperationError('Managed data directories must not be symlinks')
        (DATA/name).mkdir(mode=0o700 if name in ('plans', 'certificates') else 0o755, exist_ok=True)
    pub = pwd.getpwnam('proxyhub-pub')
    os.chown(DATA/'publish', 0, pub.pw_gid)
    (DATA/'publish').chmod(0o750)
    for name in ('client_temp', 'proxy_temp', 'fastcgi_temp', 'uwsgi_temp', 'scgi_temp'):
        if (DATA/'nginx'/name).is_symlink():
            raise OperationError('Managed temporary directories must not be symlinks')
        (DATA/'nginx'/name).mkdir(mode=0o700, exist_ok=True)
    # nginx -t is run before systemd first starts the unit. RuntimeDirectory
    # would otherwise only appear after that validation has already failed.
    runtime = Path('/run/proxyhub')
    if runtime.is_symlink():
        raise OperationError('Runtime directory must not be a symlink')
    runtime.mkdir(mode=0o755, exist_ok=True)
    for name, content in units().items():
        atomic_write(Path('/etc/systemd/system')/name, content.encode(), 0o644)
    atomic_write(launcher, b'#!/bin/sh\n# Managed by ProxyHub\nexport PYTHONPATH=/opt/proxyhub/app/src\nexec /usr/bin/python3 -m proxyhub "$@"\n', 0o755)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', 'proxyhub-renew.timer'])
    return {'installed': ['proxyhub', *units()], 'services_started': False,
            'renewal_timer': 'enabled; start after successful initial activation'}
