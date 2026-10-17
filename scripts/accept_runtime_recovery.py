"""Explicit disruptive runtime recovery acceptance on a dedicated VPS only.

Run as root, with no concurrent writers and a separately running resource sampler:
  python3 accept_runtime_recovery.py --evidence-dir /private/acceptance \
      --legacy-revision /var/lib/proxyhub/revisions/TRUSTED_OLD_REVISION

The legacy revision must contain the exact current configuration, but the old
independent HY2/node layout. Only runtime units and publisher reload are touched.
Every exit attempts to restore the exact initial revision and close any pending
transaction. Historical unexplained failures remain unexplained by this run.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

from proxyhub import deployment as d, nodes
from proxyhub.core import OperationError, atomic_write, canonical, digest, require_root
from proxyhub.state import StateStore


class CheckFailed(Exception):
    pass


def check(condition, code):
    if not condition:
        raise CheckFailed(code)


def configuration(directory):
    return json.loads((directory / 'config.json').read_text())


class Trace:
    """Process-local instrumentation; it never persists altered client credentials."""
    def __init__(self, evidence, run_id):
        self.evidence, self.run_id = evidence, run_id
        self.path = evidence / (run_id + '-probes.jsonl')
        self.layout, self.case, self.transaction = 'initial', 'initial', 'none'
        self.phase, self.operation = 'initial', 'none'
        self.candidate = None
        self.injection, self.remaining = 'none', 0
        self.attempts, self.probes, self.calls = {}, [], []
        self.originals = {name: getattr(d, name) for name in
                          ('_proxy_probe', '_runtime_health', '_isolated_runtime_health', '_restore', 'run')}

    def stage(self):
        return self.layout + '.' + self.case + '.' + self.phase

    def emit(self, event, **values):
        row = {'utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
               'monotonic': time.monotonic(), 'run_id': self.run_id,
               'transaction': self.transaction, 'layout': self.layout, 'case': self.case,
               'stage': self.stage(), 'event': event, **values}
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(json.dumps(row, separators=(',', ':')) + '\n')
            output.flush()
            os.fsync(output.fileno())
        return row

    @contextmanager
    def phase_as(self, phase):
        previous = self.phase
        self.phase = phase
        atomic_write(self.evidence / 'stage.json', canonical({
            'run_id': self.run_id, 'transaction': self.transaction, 'stage': self.stage()}))
        self.emit('phase-start')
        try:
            yield
        finally:
            self.emit('phase-end')
            self.phase = previous
            atomic_write(self.evidence / 'stage.json', canonical({
                'run_id': self.run_id, 'transaction': self.transaction, 'stage': self.stage()}))

    def probe(self, config, outbound, fingerprint=None):
        protocol = outbound['type']
        if protocol == 'hysteria2':
            member, protocol = config['hy2']['id'], 'hy2'
        else:
            member = next((node['id'] for node in nodes.normalize_nodes(config)
                           if node['port'] == outbound['port']), 'unmatched')
        verification = ('pin' if fingerprint is not None else 'reality-authentication'
                        if outbound.get('reality-opts') else 'pki'
                        if outbound.get('tls') or protocol in {'hy2', 'trojan', 'tuic', 'anytls'}
                        else 'authentication')
        key = (self.transaction, self.phase, member)
        if verification != 'pin':
            self.attempts[key] = self.attempts.get(key, 0) + 1
        attempt = self.attempts.get(key, 1)
        injected = protocol == 'hy2' and self.phase in {
            'candidate-health', 'automatic-restore-health', 'recover-health'} and (
                self.injection == 'persistent' or
                (self.injection == 'transient' and self.phase == 'candidate-health' and self.remaining > 0))
        selected = outbound
        if injected:
            selected = deepcopy(outbound)
            selected['password'] = secrets.token_urlsafe(32)
            self.remaining = max(0, self.remaining - 1)
        fields = {'member': member, 'protocol': protocol, 'verification': verification,
                  'attempt': attempt, 'injected_wrong_credentials': injected}
        self.emit('probe-start', **fields)
        started = time.monotonic()
        try:
            self.originals['_proxy_probe'](config, selected, fingerprint)
            check(not injected, 'wrong-credentials-authenticated')
        except Exception as error:
            category = ('authentication-negative-control' if injected else
                        'client-startup' if isinstance(error, OperationError) and 'could not start' in str(error) else
                        'client-listener' if isinstance(error, OperationError) and 'unavailable' in str(error) else
                        'network-authentication-or-certificate' if isinstance(error, OperationError) else
                        'unexpected-probe-error')
            row = self.emit('probe-end', **fields, elapsed_seconds=time.monotonic() - started,
                            success=False, error_class=type(error).__name__, error_category=category,
                            error='Probe failed; original potentially sensitive error text withheld')
            self.probes.append(row)
            raise
        row = self.emit('probe-end', **fields, elapsed_seconds=time.monotonic() - started,
                        success=True, error_class=None, error_category=None, error=None)
        self.probes.append(row)

    def runtime_health(self, config, directory, changed):
        if self.operation == 'crash-child' and directory == self.candidate:
            with self.phase_as('interrupted-after-runtime-switch'):
                self.emit('intentional-process-exit', exit_code=86)
                os._exit(86)
        phase = ('candidate-health' if directory == self.candidate and self.operation == 'apply'
                 else 'automatic-restore-health' if self.operation == 'apply'
                 else 'recover-health' if self.operation == 'recover' else 'fixture-health')
        with self.phase_as(phase):
            return self.originals['_runtime_health'](config, directory, changed)

    def isolated_health(self, config, directory):
        with self.phase_as('isolated-candidate-health'):
            return self.originals['_isolated_runtime_health'](config, directory)

    def restore(self, *args, **kwargs):
        with self.phase_as('automatic-restore' if self.operation == 'apply' else 'recover-restore'):
            return self.originals['_restore'](*args, **kwargs)

    def command(self, argv, *args, **kwargs):
        if argv and argv[0] == 'systemctl':
            protected = ('proxyhub-proxy.', 'proxyhub-reality@', 'proxyhub-nginx.')
            check(not any(str(value).startswith(protected) for value in argv[2:]),
                  'protected-service-action-refused')
            self.calls.append(tuple(argv))
            self.emit('runtime-service-command', action=argv[1],
                      unit_roles=['publisher' if value == 'proxyhub-publisher.service' else
                                  'shared' if value == 'proxyhub-shared.service' else
                                  'hy2' if value == 'proxyhub-hy2.service' else 'node'
                                  for value in argv[2:]])
        return self.originals['run'](argv, *args, **kwargs)

    def install(self):
        d._proxy_probe, d._runtime_health = self.probe, self.runtime_health
        d._isolated_runtime_health, d._restore, d.run = self.isolated_health, self.restore, self.command

    def uninstall(self):
        for name, value in self.originals.items():
            setattr(d, name, value)


class Acceptance:
    def __init__(self, evidence, legacy, run_id):
        self.evidence, self.legacy = evidence, legacy
        self.store = StateStore(d.ROOT / 'plans')
        check(not self.store.pending(), 'preexisting-pending-activation')
        self.baseline = d._current_revision()
        check(self.baseline is not None and (self.baseline / 'shared.json').is_file(),
              'initial-layout-must-be-shared')
        self.config = configuration(self.baseline)
        check(not (legacy / 'shared.json').exists(), 'legacy-revision-must-be-independent')
        check(configuration(legacy) == self.config, 'legacy-configuration-differs-from-baseline')
        check(d._hy2_enabled(self.config), 'enabled-hy2-required-for-real-authentication-negative-control')
        self.node = next((node for node in nodes.normalize_nodes(self.config) if node['enabled']), None)
        check(self.node is not None, 'enabled-extra-node-required')
        self.trace = Trace(evidence, run_id)
        self.results = []

    def fixture(self, target):
        self.trace.injection, self.trace.operation = 'none', 'fixture'
        with self.trace.phase_as('fixture-switch'):
            previous = d._current_revision()
            config, old = configuration(target), configuration(previous)
            with self.store.exclusive():
                check(not self.store.pending(), 'fixture-refuses-pending-activation')
                d._link(target)
                d._publish(target)
                changed = d._sync_runtime(config, old, target, previous)
                # Explicitly authenticate every member even if this is an idempotent fixture.
                d._runtime_health(config, target, (d._hy2_enabled(config),
                    {node['id'] for node in nodes.normalize_nodes(config) if node['enabled']}))
                d.run(['systemctl', 'reload', 'proxyhub-publisher.service'])
                d._wait_publication(target)
                check(d._current_revision() == target, 'fixture-pointer-mismatch')

    def candidate(self, layout):
        self.trace.operation, self.trace.injection = 'prepare', 'none'
        with self.trace.phase_as('prepare'):
            config = configuration(d._current_revision())
            if layout == 'legacy':
                plan = d.prepare(self.store, config, runtime_only=True)
            else:
                occupied = {node['port'] for node in nodes.normalize_nodes(config)}
                occupied.add(config['hy2']['port'])
                for _ in range(100):
                    with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                        tcp.bind(('127.0.0.1', 0))
                        port = tcp.getsockname()[1]
                        if port in occupied:
                            continue
                        try:
                            udp.bind(('127.0.0.1', port))
                        except OSError:
                            continue
                        break
                else:
                    raise CheckFailed('no-free-temporary-node-port')
                changed = nodes.change(config, 'nodes.update', {'node_id': self.node['id'], 'port': port})
                plan = d.prepare(self.store, changed,
                                 node_operation={'kind': 'update', 'node_id': self.node['id']})
            self.trace.transaction = plan['plan_id']
            self.trace.candidate = d.ROOT / 'revisions' / plan['revision']
            self.trace.emit('prepared', activation=plan['activation'])
            return plan

    def all_members_passed(self, records, phase):
        expected = {self.config['hy2']['id']: {'pki', 'pin'}}
        for node in nodes.normalize_nodes(self.config):
            if node['enabled']:
                options = node['options']
                expected[node['id']] = ({'pki', 'pin'} if options.get('tls_mode') == 'certificate' else
                    {'reality-authentication'} if options.get('tls_mode') == 'reality' else
                    {'pki'} if options.get('tls') else {'authentication'})
        passed = {}
        for row in records:
            if row['success'] and row['stage'].endswith('.' + phase):
                passed.setdefault(row['member'], set()).add(row['verification'])
        check(passed == expected, 'not-all-runtime-members-and-verification-modes-authenticated')

    def recover(self):
        self.trace.operation = 'recover'
        with self.trace.phase_as('recover'):
            return d.recover(self.store)

    def case(self, layout, kind):
        started = time.monotonic()
        self.trace.layout, self.trace.case, self.trace.transaction = layout, kind, 'none'
        start = len(self.trace.probes)
        source = self.legacy if layout == 'legacy' else self.baseline
        self.fixture(source)
        plan = self.candidate(layout)
        if kind == 'transient':
            self.trace.operation, self.trace.injection, self.trace.remaining = 'apply', 'transient', 2
            with self.trace.phase_as('apply'):
                check(d.apply(self.store, plan['plan_id'])['status'] == 'applied', 'transient-apply-failed')
            records = self.trace.probes[start:]
            injected = [row for row in records if row['injected_wrong_credentials']]
            check(len(injected) == 2 and all(not row['success'] for row in injected),
                  'transient-negative-controls-not-observed')
            self.all_members_passed(records, 'candidate-health')
            check(not self.store.pending(), 'transient-left-pending')
            check(self.store.inspect(plan['plan_id'])['status'] == 'applied', 'transient-not-committed')
            result = {'layout': layout, 'case': kind, 'wrong_credential_failures': 2,
                      'all_members_healthy_before_commit': True, 'pending': False}
        elif kind == 'persistent':
            self.trace.operation, self.trace.injection = 'apply', 'persistent'
            try:
                with self.trace.phase_as('apply'):
                    d.apply(self.store, plan['plan_id'])
            except OperationError:
                pass
            else:
                raise CheckFailed('persistent-apply-reported-success')
            check(len(self.store.pending()) == 1, 'failed-auto-restore-cleared-pending')
            check(self.store.inspect(plan['plan_id'])['status'] == 'prepared', 'failed-auto-restore-committed')
            for phase in ('candidate-health', 'automatic-restore-health'):
                failed = [row for row in self.trace.probes[start:]
                          if row['stage'].endswith('.' + phase) and row['injected_wrong_credentials']]
                check(len(failed) == 3 and all(not row['success'] for row in failed),
                      'persistent-bounded-attempts-not-observed')
            try:
                self.recover()
            except OperationError:
                pass
            else:
                raise CheckFailed('persistent-recover-reported-success')
            check(len(self.store.pending()) == 1, 'failed-recover-cleared-pending')
            check(self.store.inspect(plan['plan_id'])['status'] == 'prepared', 'failed-recover-committed')
            failed = [row for row in self.trace.probes[start:]
                      if row['stage'].endswith('.recover-health') and row['injected_wrong_credentials']]
            check(len(failed) == 3 and all(not row['success'] for row in failed),
                  'persistent-recover-attempts-not-observed')
            self.trace.injection = 'none'
            resumed = len(self.trace.probes)
            check(self.recover()['status'] == 'recovered', 'released-recover-failed')
            self.all_members_passed(self.trace.probes[resumed:], 'recover-health')
            check(not self.store.pending() and d._current_revision() == source, 'recovery-state-mismatch')
            actions = len(self.trace.calls), len(self.trace.probes)
            check(self.recover()['status'] == 'nothing_to_recover', 'recovery-not-idempotent')
            check((len(self.trace.calls), len(self.trace.probes)) == actions, 'idempotent-recovery-performed-actions')
            result = {'layout': layout, 'case': kind, 'apply_and_auto_restore_failed': True,
                      'failed_recover_kept_pending': True, 'all_members_healthy_after_release': True,
                      'recovery_idempotent': True, 'pending': False}
        else:
            self.trace.emit('child-start')
            self.store.db.close()
            try:
                child = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                    '--evidence-dir', str(self.evidence), '--legacy-revision', str(self.legacy),
                    '--child-plan', plan['plan_id'], '--run-id', self.trace.run_id, '--child-layout', layout],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            finally:
                self.store = StateStore(d.ROOT / 'plans')
            check(child.returncode == 86, 'child-did-not-exit-at-runtime-boundary')
            check(len(self.store.pending()) == 1, 'child-interruption-missing-pending')
            check(d._current_revision() == self.trace.candidate, 'child-not-at-candidate-pointer')
            resumed = len(self.trace.probes)
            check(self.recover()['status'] == 'recovered', 'interrupted-recovery-failed')
            self.all_members_passed(self.trace.probes[resumed:], 'recover-health')
            check(not self.store.pending() and d._current_revision() == source, 'interrupted-state-mismatch')
            actions = len(self.trace.calls), len(self.trace.probes)
            check(self.recover()['status'] == 'nothing_to_recover', 'interrupted-recovery-not-idempotent')
            check((len(self.trace.calls), len(self.trace.probes)) == actions, 'idempotent-recovery-performed-actions')
            result = {'layout': layout, 'case': kind, 'child_exit_code': 86,
                      'all_members_healthy_after_recovery': True, 'recovery_idempotent': True, 'pending': False}
        result['elapsed_seconds'] = time.monotonic() - started
        self.trace.emit('case-passed', **result)
        self.results.append(result)

    def run(self):
        result = {'schema': 1, 'run_id': self.trace.run_id, 'completed': False,
                  'baseline_revision': self.baseline.name, 'legacy_revision': self.legacy.name,
                  'baseline_config_sha256': digest(canonical(self.config)), 'cases': self.results,
                  'historical_first_pending_root_cause': 'unknown; this run does not reconstruct historical evidence',
                  'protected_service_actions': 0, 'probe_evidence': self.trace.path.name}
        self.trace.install()
        failure = None
        try:
            for layout in ('legacy', 'shared'):
                for kind in ('transient', 'persistent', 'interruption'):
                    self.case(layout, kind)
            result['completed'] = True
        except Exception as error:
            failure = error
            result['failure_class'] = type(error).__name__
            result['failure_code'] = str(error) if isinstance(error, CheckFailed) else 'runtime-operation-failed'
            self.trace.emit('run-failed', error_class=result['failure_class'], error=result['failure_code'])
        finally:
            self.trace.layout, self.trace.case, self.trace.transaction = 'cleanup', 'baseline', 'none'
            self.trace.injection = 'none'
            try:
                if self.store.pending():
                    check(self.recover()['status'] == 'recovered', 'cleanup-recover-failed')
                self.fixture(self.baseline)
                check(not self.store.pending(), 'cleanup-left-pending')
                check(configuration(d._current_revision()) == self.config, 'cleanup-configuration-mismatch')
                result.update(baseline_restored=True, pending=False)
            except Exception as error:
                result.update(completed=False, baseline_restored=False,
                              cleanup_failure_class=type(error).__name__,
                              cleanup_failure_code=str(error) if isinstance(error, CheckFailed) else 'runtime-cleanup-failed')
                self.trace.emit('cleanup-failed', error_class=type(error).__name__, error='Baseline restoration failed')
                failure = failure or error
            finally:
                self.trace.uninstall()
                self.store.db.close()
            atomic_write(self.evidence / (self.trace.run_id + '-result.json'), canonical(result))
        return result, failure


def child_run(args, evidence, run_id):
    store = StateStore(d.ROOT / 'plans')
    trace = Trace(evidence, run_id)
    trace.layout, trace.case, trace.transaction = args.child_layout, 'interruption', args.child_plan
    trace.operation = 'crash-child'
    trace.candidate = d.ROOT / 'revisions' / store.inspect(args.child_plan)['payload']['revision']
    trace.install()
    try:
        d.apply(store, args.child_plan)
        return 2
    except Exception as error:
        trace.emit('child-failed-before-boundary', error_class=type(error).__name__, error='Child activation failed')
        return 1
    finally:
        trace.uninstall()
        store.db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--legacy-revision', type=Path, required=True)
    parser.add_argument('--child-plan', help=argparse.SUPPRESS)
    parser.add_argument('--child-layout', choices=('legacy', 'shared'), help=argparse.SUPPRESS)
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    require_root()
    check(args.evidence_dir.is_dir() and not args.evidence_dir.is_symlink(), 'private-evidence-directory-required')
    evidence = args.evidence_dir.resolve()
    check(evidence.stat().st_uid == 0 and evidence.stat().st_mode & 0o077 == 0, 'evidence-directory-not-root-private')
    legacy = d._managed_revision(args.legacy_revision)
    run_id = args.run_id or 'runtime-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3)
    check(all(character.isalnum() or character in '._-' for character in run_id), 'unsafe-run-id')
    if args.child_plan:
        return child_run(args, evidence, run_id)
    result, failure = Acceptance(evidence, legacy, run_id).run()
    print(json.dumps({'run_id': run_id, 'completed': result['completed'],
                      'baseline_restored': result.get('baseline_restored', False),
                      'result': run_id + '-result.json', 'probes': run_id + '-probes.jsonl'}))
    return int(failure is not None)


if __name__ == '__main__':
    raise SystemExit(main())
