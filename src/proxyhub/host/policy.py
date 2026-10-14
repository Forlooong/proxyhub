"""Enable public-key authentication without changing any other SSH policy."""
from pathlib import Path

from proxyhub.core import OperationError, atomic_write, digest, run

DROPIN = Path('/etc/ssh/sshd_config.d/00-proxyhub-publickey.conf')
CONTENT = b'# Managed by ProxyHub: public key authentication only\nPubkeyAuthentication yes\n'


def enable_key_auth(*, apply: bool = False) -> dict:
    before = run(['/usr/sbin/sshd', '-T'])
    selected = {line.split(' ', 1)[0]: line.split(' ', 1)[1] for line in before.splitlines() if ' ' in line}
    if selected.get('pubkeyauthentication') == 'yes':
        return {'status': 'unchanged', 'public_key_authentication': True}
    if DROPIN.exists() or DROPIN.is_symlink():
        raise OperationError('Public-key drop-in already exists; inspect it before continuing')
    if not apply:
        return {'status': 'planned', 'change': 'PubkeyAuthentication yes', 'password_login': 'unchanged', 'port': 'unchanged', 'root_policy': 'unchanged'}
    atomic_write(DROPIN, CONTENT, 0o600)
    try:
        run(['/usr/sbin/sshd', '-t'])
        after = dict(line.split(' ', 1) for line in run(['/usr/sbin/sshd', '-T']).splitlines() if ' ' in line)
        if after.get('pubkeyauthentication') != 'yes':
            raise OperationError('Drop-in did not enable effective public-key authentication')
        for key in ('passwordauthentication', 'permitrootlogin', 'port', 'kbdinteractiveauthentication'):
            if after.get(key) != selected.get(key):
                raise OperationError('Unexpected SSH policy change')
        run(['systemctl', 'reload', 'ssh.service'])
    except Exception:
        if DROPIN.exists() and digest(DROPIN.read_bytes()) == digest(CONTENT):
            DROPIN.unlink()
            run(['/usr/sbin/sshd', '-t'])
            run(['systemctl', 'reload', 'ssh.service'])
        raise
    return {'status': 'enabled', 'login_verification': 'required', 'password_login': 'unchanged', 'port': 'unchanged'}
