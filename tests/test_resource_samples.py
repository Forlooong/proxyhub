"""Small evidence-contract checks; no server or process is started."""
import importlib.util
import json
from pathlib import Path
import shutil


spec = importlib.util.spec_from_file_location('sample_resources', Path(__file__).parents[1] /
                                             'scripts' / 'sample_resources.py')
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)


def process(proc, pid, parent=1, start=100, pages=2):
    directory = proc / str(pid)
    directory.mkdir(exist_ok=True)
    fields = ['S', str(parent)] + ['0'] * 17 + [str(start), '0', str(pages)]
    (directory / 'stat').write_text(f'{pid} (comm with ) spaces) ' + ' '.join(fields))
    # Deliberately differs from stat.rss_pages * 4 to forbid a silent fallback.
    (directory / 'smaps_rollup').write_text(f'Rss: {pages * 4 + 1} kB\n')


def sampler(tmp_path, monkeypatch):
    proc, cgroup = tmp_path / 'proc', tmp_path / 'cgroup'
    (proc / 'sys/kernel/random').mkdir(parents=True)
    (proc / 'sys/kernel/random/boot_id').write_text('test-boot')
    (proc / 'vmstat').write_text('oom_kill 7\n')
    nested = cgroup / r'system-proxyhub\x2dnode.slice/proxyhub-node@id.service/nested'
    nested.mkdir(parents=True)
    (nested / 'cgroup.procs').write_text('101\n')
    unrelated = cgroup / 'unrelated.service'
    unrelated.mkdir()
    (unrelated / 'cgroup.procs').write_text('404\n')
    result = resources.Sampler(proc=proc, cgroup=cgroup, sampler_pid=999)
    monkeypatch.setattr(result, 'role', lambda pid: 'validation' if pid == 202 else None)
    for pid in (101, 202, 404, 999):
        process(proc, pid)
    process(proc, 203, parent=202)
    return result, proc


def test_recursive_cgroups_descendants_pid_reuse_and_sampler_exclusion(tmp_path, monkeypatch):
    capture, proc = sampler(tmp_path, monkeypatch)
    initial = capture.sample()
    assert {item['pid']: item['role'] for item in initial['processes']} == {
        101: 'service', 202: 'validation', 203: 'related-child'}
    assert initial['total_rss_kib'] == 27
    assert initial['service_rss_kib'] == initial['sampler_rss_kib'] == 9
    assert initial['rss_complete']
    process(proc, 101, start=101)
    shutil.rmtree(proc / '202')
    (proc / '404/stat').write_text('broken')
    following = capture.sample()
    assert following['exited'] == [
        {'pid': 101, 'start_ticks': 100, 'role': 'service', 'reason': 'pid-reused'},
        {'pid': 202, 'start_ticks': 100, 'role': 'validation', 'reason': 'exited-between-samples'}]
    assert following['unread'] == [{'pid': 404, 'error': 'invalid-data'}]
    assert following['total_rss_kib'] == 18
    assert not following['rss_complete']


def test_stage_rejects_unsafe_values_without_echoing_them(tmp_path):
    stage = tmp_path / 'stage'
    stage.write_text('{"stage":"candidate-health","run_id":"run-2","transaction":"txn:4"}')
    assert resources.stage_value(stage) == {'stage': 'candidate-health', 'run_id': 'run-2',
                                            'transaction': 'txn:4'}
    stage.write_text('{"stage":"private.domain/secret-value"}')
    assert resources.stage_value(stage) == {'stage': 'unavailable', 'stage_error': 'invalid-data'}


def test_missing_smaps_and_pid_reuse_do_not_fall_back_to_stat_rss(tmp_path, monkeypatch):
    capture, proc = sampler(tmp_path, monkeypatch)
    (proc / '101/smaps_rollup').unlink()
    original = resources.read_rss
    def replace_pid_after_read(path):
        value = original(path)
        if path.parent.name == '202':
            process(proc, 202, start=101)
        return value
    monkeypatch.setattr(resources, 'read_rss', replace_pid_after_read)
    sample = capture.sample()
    assert sample['unread'] == [{'pid': 101, 'error': 'exited'}, {'pid': 202, 'error': 'pid-reused'}]
    assert [item['pid'] for item in sample['processes']] == [203]
    assert sample['total_rss_kib'] == 9
    assert sample['service_rss_kib'] == 0
    assert not sample['rss_complete']


def test_summary_recomputes_rss_and_same_boot_oom_delta(tmp_path, monkeypatch):
    capture, proc = sampler(tmp_path, monkeypatch)
    first = capture.sample()
    process(proc, 101, pages=3)
    (proc / 'vmstat').write_text('oom_kill 8\n')
    second = capture.sample()
    # The raw rows, not a trusted precomputed maximum, determine the result.
    second['total_rss_kib'] = 0
    raw = tmp_path / 'samples.jsonl'
    raw.write_text('\n'.join(json.dumps(record) for record in (first, second)) + '\n{')
    summary = resources.summarize(raw)
    assert summary['samples'] == 2
    assert summary['total_peak_kib'] == 31
    assert summary['service_peak_kib'] == 13
    assert summary['total_mismatches'] == summary['malformed_lines'] == 1
    assert summary['single_boot']
    assert summary['boots']['test-boot']['oom_kill_delta'] == 1
