import { useEffect, useState } from 'react';
import { message, operation } from '../api';
import type { Job } from '../types';
import { date, display, Notice, Section, Status } from '../components/common';
import { Icon } from '../components/Icon';

export function JobsPage({activeId, activeStatus, recover, busy}: {activeId?: string; activeStatus?: string; recover: () => void; busy: boolean}) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  async function refresh() {
    setLoading(true); setError('');
    try {const result = await operation<{jobs: Job[]}>('jobs.list'); setJobs(result.jobs);}
    catch (error) {setError(message(error));} finally {setLoading(false);}
  }
  useEffect(() => {void refresh();}, [activeId, activeStatus]);
  useEffect(() => {
    if (!jobs.some(job => ['queued', 'running'].includes(job.status))) return;
    const timer = window.setTimeout(() => void refresh(), 3000);
    return () => window.clearTimeout(timer);
  }, [jobs]);
  return <>
    <Section title="任务记录" description="预览与应用分别记录。刷新页面不会取消服务器上的任务。" action={<button className="text-button" disabled={loading} onClick={() => void refresh()}><Icon name="refresh" />刷新记录</button>}>
      {error && <Notice tone="error">{error}</Notice>}
      {loading && jobs.length === 0 && <p className="empty" role="status">正在读取任务…</p>}
      {!loading && !jobs.length && !error && <p className="empty">还没有任务。创建变更预览后，可在这里查看状态。</p>}
      <div className="job-list">{jobs.map(job => <details className={`job-item ${job.id === activeId ? 'job-active' : ''}`} key={job.id}><summary><span className="job-title"><strong>{job.operation}</strong><small>{date(job.created_at)}</small></span><Status value={job.status} /></summary><div className="job-detail"><dl><dt>任务 ID</dt><dd><code>{job.id}</code></dd><dt>结束时间</dt><dd>{date(job.finished_at)}</dd></dl>{job.error && <Notice tone="error">{typeof job.error === 'string' ? job.error : job.error.message ?? '任务未完成，请重新预览。'}</Notice>}{job.result && <pre tabIndex={0}>{display(job.result)}</pre>}{job.status === 'interrupted' && <p className="muted">管理服务重启时此任务尚未结束。先核对服务与部署状态，再决定重新预览或恢复。</p>}</div></details>)}</div>
    </Section>
    <Section title="恢复中断的部署" description="读取部署日志，并恢复到已提交的有效版本。正常运行时不会修改部署。"><button className="button secondary" onClick={recover} disabled={busy}><Icon name="refresh" />检查并恢复部署</button></Section>
  </>;
}
