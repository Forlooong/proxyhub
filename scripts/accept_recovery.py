"""Explicit disruptive recovery acceptance on a dedicated ProxyHub test host.

Run as root only on a test deployment. It restarts ProxyHub services, injects a
health-check failure, then simulates a crash after the current-link switch.
"""
import json
from pathlib import Path
import subprocess
import sys

from proxyhub import deployment
from proxyhub.core import OperationError, require_root
from proxyhub.state import StateStore

require_root()
store = StateStore(Path('/var/lib/proxyhub/plans'))
previous = (deployment.ROOT/'current').resolve()
config = json.loads((previous/'config.json').read_text())
original_health = deployment._health
plan = deployment.prepare(store, config)

remaining_failures = 15
def fail(server):
    global remaining_failures
    if remaining_failures:
        remaining_failures -= 1
        raise OperationError('Injected acceptance failure')
    original_health(server)

deployment._health = fail
try:
    deployment.apply(store, plan['plan_id'])
    raise AssertionError('Injected failure was unexpectedly accepted')
except OperationError:
    assert store.inspect(plan['plan_id'])['status'] == 'failed'
finally:
    deployment._health = original_health
assert (deployment.ROOT/'current').resolve() == previous
original_health(config['server'])
plan = deployment.prepare(store, config)
child = '''
import os, sys
from pathlib import Path
from proxyhub import deployment
from proxyhub.state import StateStore
store = StateStore(Path('/var/lib/proxyhub/plans'))
with store.exclusive():
    _, payload = store.get(sys.argv[1])
    candidate = deployment.ROOT/'revisions'/payload['revision']
    previous = (deployment.ROOT/'current').resolve()
    store.record(sys.argv[1], 'activating', {'previous':str(previous),'candidate':str(candidate)})
    deployment._link(candidate)
    os._exit(86)
'''
result = subprocess.run([sys.executable,'-c',child,plan['plan_id']],capture_output=True)
assert result.returncode == 86
assert store.pending()
assert deployment.recover(store)['status'] == 'recovered'
assert (deployment.ROOT/'current').resolve() == previous
assert deployment.recover(store)['status'] == 'nothing_to_recover'
original_health(config['server'])
print(json.dumps({'failed_activation_restored_previous':True,'crash_recovery':True,'recovery_idempotent':True}))
