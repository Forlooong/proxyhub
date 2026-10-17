"""Linux RSS evidence for dedicated ProxyHub acceptance; no command lines are written.

Capture: python3 sample_resources.py --output /private/rss.jsonl --interval .2 \
    --validation-root /opt/proxyhub/shared-runtime-validation \
    --stage-file /private/stage.json --stop-file /private/stop
Recompute: python3 sample_resources.py --summary /private/rss.jsonl

RSS strictly uses smaps_rollup.Rss, not PSS: shared pages can be counted more than
once. Samples cannot observe processes born and exited between samples. The
sampler's own RSS is reported separately and excluded from both reported totals.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys
import time


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def error_kind(error):
    if isinstance(error, (FileNotFoundError, ProcessLookupError)):
        return 'exited'
    if isinstance(error, PermissionError):
        return 'permission-denied'
    if isinstance(error, (ValueError, IndexError, UnicodeError)):
        return 'invalid-data'
    return 'os-error'


def read_stat(path):
    # comm may contain spaces and parentheses; fields after its final ')' are fixed.
    fields = path.read_text().rpartition(')')[2].split()
    return {'ppid': int(fields[1]), 'start_ticks': int(fields[19])}


def read_rss(path):
    for line in path.read_text().splitlines():
        if line.startswith('Rss:'):
            fields = line.split()
            if len(fields) != 3 or fields[2] != 'kB' or int(fields[1]) < 0:
                raise ValueError()
            return int(fields[1])
    raise ValueError()


def stage_value(path):
    if path is None:
        return {'stage': 'unlabeled'}
    try:
        raw = path.read_text()
        if len(raw) > 4096:
            raise ValueError()
        value = json.loads(raw) if raw.lstrip().startswith('{') else {'stage': raw.strip()}
        result = {}
        for key in ('run_id', 'transaction', 'stage'):
            item = value.get(key)
            if item is not None:
                if not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', item):
                    raise ValueError()
                result[key] = item
        return result or {'stage': 'unlabeled'}
    except (OSError, ValueError, TypeError, AttributeError) as error:
        return {'stage': 'unavailable', 'stage_error': error_kind(error)}


def service_pids(root):
    """Walk the whole cgroup tree, including template slices and nested children."""
    pids, errors = set(), Counter()
    def walk_error(error):
        errors[error_kind(error)] += 1
    for directory, _, files in os.walk(root, onerror=walk_error):
        if 'cgroup.procs' not in files or not any('proxyhub' in part.lower()
                                                  for part in Path(directory).relative_to(root).parts):
            continue
        try:
            pids.update(int(value) for value in (Path(directory) / 'cgroup.procs').read_text().split())
        except (OSError, ValueError) as error:
            errors[error_kind(error)] += 1
    return pids, dict(errors)


class Sampler:
    def __init__(self, validation_roots=(), include_pids=(), *, proc=Path('/proc'),
                 cgroup=Path('/sys/fs/cgroup'), sampler_pid=None):
        self.proc, self.cgroup = proc, cgroup
        self.pid = os.getpid() if sampler_pid is None else sampler_pid
        self.roots = tuple(str(Path(root).resolve()).encode() for root in validation_roots)
        self.include_pids = set(include_pids)
        self.previous = {}
        self.boot_id = (proc / 'sys/kernel/random/boot_id').read_text().strip()

    def role(self, pid):
        # Match NUL-delimited arguments only in memory; never emit argv or exe paths.
        args = (self.proc / str(pid) / 'cmdline').read_bytes().split(b'\0')
        try:
            name = (self.proc / str(pid) / 'exe').readlink().name.lower()
        except OSError:
            name = Path(os.fsdecode(args[0])).name.lower() if args else ''
        if name.startswith(('sing-box', 'sing_box')):
            return 'core'
        if name.startswith(('mihomo', 'clash-meta', 'clash_meta')):
            return 'validation-client'
        if any(root in arg for root in self.roots for arg in args):
            return 'validation'
        if name.startswith('node') and any(b'/opt/proxyhub/' in arg or b'/var/lib/proxyhub/' in arg
                                            for arg in args):
            return 'converter'
        return None

    def sample(self, stage_file=None):
        started = time.monotonic()
        services, cgroup_errors = service_pids(self.cgroup)
        records, errors = {}, {}
        for entry in self.proc.iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                records[pid] = read_stat(entry / 'stat')
            except (OSError, ValueError, IndexError, UnicodeError) as error:
                errors[pid] = error_kind(error)
        roles, selector_errors = {}, {}
        for pid, record in records.items():
            if pid == self.pid:
                continue
            if pid in services:
                roles[pid] = 'service'
            elif pid in self.include_pids:
                roles[pid] = 'validation-root'
            elif (pid, record['start_ticks']) in self.previous:
                roles[pid] = self.previous[(pid, record['start_ticks'])]
            else:
                try:
                    role = self.role(pid)
                    if role:
                        roles[pid] = role
                except (OSError, ValueError, UnicodeError) as error:
                    selector_errors[pid] = error_kind(error)
        # Descendants cover subprocesses such as openssl/curl and Python probes.
        while True:
            children = {pid: 'related-child' for pid, record in records.items()
                        if pid != self.pid and pid not in roles and record['ppid'] in roles}
            if not children:
                break
            roles.update(children)
        rss = {}
        for pid in (*roles, self.pid):
            if pid not in records:
                continue
            try:
                value = read_rss(self.proc / str(pid) / 'smaps_rollup')
                if read_stat(self.proc / str(pid) / 'stat')['start_ticks'] != records[pid]['start_ticks']:
                    errors[pid] = 'pid-reused'
                    continue
                rss[pid] = value
            except (OSError, ValueError, IndexError, UnicodeError) as error:
                errors[pid] = error_kind(error)
        current = {(pid, records[pid]['start_ticks']): roles[pid] for pid in roles}
        exited = []
        for (pid, start), role in sorted(self.previous.items()):
            if (pid, start) not in current:
                reason = ('pid-reused' if pid in records else errors.get(pid, 'exited-between-samples'))
                exited.append({'pid': pid, 'start_ticks': start, 'role': role, 'reason': reason})
        self.previous = current
        processes = [{'pid': pid, 'start_ticks': records[pid]['start_ticks'], 'role': roles[pid],
                      'service': pid in services, 'rss_kib': rss[pid]}
                     for pid in sorted(roles) if pid in rss]
        try:
            oom = next(int(line.split()[1]) for line in (self.proc / 'vmstat').read_text().splitlines()
                       if line.startswith('oom_kill '))
            oom_error = None
        except (OSError, ValueError, IndexError, StopIteration) as error:
            oom, oom_error = None, error_kind(error)
        return {'type': 'sample', 'utc': utc_now(), 'monotonic': started, 'boot_id': self.boot_id,
                **stage_value(stage_file), 'processes': processes, 'exited': exited,
                'unread': [{'pid': pid, 'error': kind} for pid, kind in sorted(errors.items())],
                'selector_unread': [{'pid': pid, 'error': kind} for pid, kind in sorted(selector_errors.items())],
                'cgroup_errors': cgroup_errors, 'oom_kill': oom, 'oom_error': oom_error,
                'service_rss_kib': sum(item['rss_kib'] for item in processes if item['service']),
                'total_rss_kib': sum(item['rss_kib'] for item in processes),
                'sampler_rss_kib': rss.get(self.pid), 'rss_source': 'smaps_rollup.Rss',
                'rss_complete': not errors and not selector_errors and not cgroup_errors,
                'sampler_excluded': True, 'scan_seconds': time.monotonic() - started}


def summarize(path):
    groups, boots, counter_errors = {}, {}, 0
    samples, malformed, mismatch, missing, incomplete = 0, 0, 0, Counter(), 0
    first, last, max_gap = None, None, 0
    with path.open() as source:
        for line in source:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                malformed += 1
                continue
            if record.get('type') != 'sample':
                continue
            samples += 1
            incomplete += not record['rss_complete']
            rss = sum(item['rss_kib'] for item in record['processes'])
            service = sum(item['rss_kib'] for item in record['processes'] if item['service'])
            if rss != record['total_rss_kib'] or service != record['service_rss_kib']:
                mismatch += 1
            key = (record.get('run_id'), record.get('transaction'), record['stage'])
            group = groups.setdefault(key, {'run_id': key[0], 'transaction': key[1], 'stage': key[2],
                                           'samples': 0, 'service_peak_kib': 0, 'total_peak_kib': 0,
                                           'scan_max_seconds': 0, 'unread': 0, 'selector_unread': 0,
                                           'cgroup_errors': 0})
            group['samples'] += 1
            group['service_peak_kib'] = max(group['service_peak_kib'], service)
            group['total_peak_kib'] = max(group['total_peak_kib'], rss)
            group['scan_max_seconds'] = max(group['scan_max_seconds'], record['scan_seconds'])
            group['unread'] += len(record['unread'])
            group['selector_unread'] += len(record['selector_unread'])
            group['cgroup_errors'] += sum(record['cgroup_errors'].values())
            missing.update(item['error'] for item in record['unread'])
            boot = boots.setdefault(record['boot_id'], {'first_oom_kill': None, 'last_oom_kill': None,
                                                        'counter_decreased': False})
            value = record['oom_kill']
            if value is None:
                counter_errors += 1
            else:
                if boot['first_oom_kill'] is None:
                    boot['first_oom_kill'] = value
                if boot['last_oom_kill'] is not None and value < boot['last_oom_kill']:
                    boot['counter_decreased'] = True
                boot['last_oom_kill'] = value
            if last is not None:
                max_gap = max(max_gap, record['monotonic'] - last)
            first = record['monotonic'] if first is None else first
            last = record['monotonic']
    for boot in boots.values():
        boot['oom_kill_delta'] = (boot['last_oom_kill'] - boot['first_oom_kill']
                                 if boot['first_oom_kill'] is not None and not boot['counter_decreased'] else None)
    return {'schema': 1, 'samples': samples, 'malformed_lines': malformed, 'total_mismatches': mismatch,
            'duration_seconds': last - first if first is not None else 0, 'max_gap_seconds': max_gap,
            'service_peak_kib': max((group['service_peak_kib'] for group in groups.values()), default=0),
            'total_peak_kib': max((group['total_peak_kib'] for group in groups.values()), default=0),
            'unread_by_reason': dict(missing), 'rss_incomplete_samples': incomplete,
            'rss_source': 'smaps_rollup.Rss', 'oom_read_errors': counter_errors, 'boots': boots,
            'single_boot': len(boots) == 1, 'sampler_excluded': True, 'stages': list(groups.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--interval', type=float, default=.2)
    parser.add_argument('--duration', type=float)
    parser.add_argument('--validation-root', type=Path, action='append', default=[])
    parser.add_argument('--include-pid', type=int, action='append', default=[])
    parser.add_argument('--stage-file', type=Path)
    parser.add_argument('--stop-file', type=Path)
    args = parser.parse_args(argv)
    if args.summary:
        result = summarize(args.summary)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(not result['samples'] or result['malformed_lines'] or result['total_mismatches'])
    if not args.output or args.interval <= 0 or (args.duration is not None and args.duration <= 0):
        parser.error('--output and positive --interval/--duration are required for capture')
    if sys.platform != 'linux':
        parser.error('capture requires Linux /proc and cgroupfs; summary is portable')
    sampler = Sampler(args.validation_root, args.include_pid)
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    started, count = time.monotonic(), 0
    # Refuse overwrite and create root-private evidence without a chmod race.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', buffering=1) as output:
        output.write(json.dumps({'type': 'metadata', 'schema': 1, 'utc': utc_now(),
                                'interval_seconds': args.interval, 'sampler_pid': sampler.pid,
                                'sampler_excluded': True, 'rss_basis': 'smaps_rollup.Rss-sum-not-PSS',
                                'selection': 'recursive-proxyhub-cgroups;all-sing-box-and-mihomo;'
                                             'proxyhub-node;validation-roots;explicit-pids;descendants',
                                'limitation': 'processes entirely between samples may be missed'}) + '\n')
        while not stopped:
            output.write(json.dumps(sampler.sample(args.stage_file), separators=(',', ':')) + '\n')
            count += 1
            if ((args.stop_file and args.stop_file.exists()) or
                    (args.duration is not None and time.monotonic() - started >= args.duration)):
                break
            time.sleep(max(0, started + count * args.interval - time.monotonic()))
        output.write(json.dumps({'type': 'end', 'utc': utc_now(), 'samples': count,
                                'signal_stop': stopped}) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
