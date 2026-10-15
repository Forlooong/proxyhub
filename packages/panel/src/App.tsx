import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, awaitJob, logout, message, operation, session } from './api';
import type { Job, JsonObject, Overview, Page, Preview, Theme } from './types';
import { Shell } from './components/Shell';
import { Login } from './components/Login';
import { PreviewDrawer } from './components/PreviewDrawer';
import { CommandMenu } from './components/CommandMenu';
import { Notice, Status } from './components/common';
import { Icon } from './components/Icon';
import { OverviewPage } from './pages/OverviewPage';
import { ServerPage } from './pages/ServerPage';
import { SitePage } from './pages/SitePage';
import { ProxyPage } from './pages/ProxyPage';
import { SubscriptionPage } from './pages/SubscriptionPage';
import { JobsPage } from './pages/JobsPage';
import { SettingsPage } from './pages/SettingsPage';

function initialTheme(): Theme {
  try {const value = localStorage.getItem('proxyhub-theme'); if (value === 'dark' || value === 'light') return value;} catch { /* Private browsing may disable storage. */ }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [page, setPage] = useState<Page>('overview');
  const [data, setData] = useState<Overview | null>(null);
  const [selectedSiteId, setSelectedSiteId] = useState('default');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [activeJob, setActiveJob] = useState<Job | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [menu, setMenu] = useState(false);
  const [toast, setToast] = useState('');
  const [outcome, setOutcome] = useState('');
  const [loggingOut, setLoggingOut] = useState(false);
  const authEpoch = useRef(0);
  const notify = useCallback((text: string) => setToast(text), []);
  const navigate = useCallback((target: Page) => {setPage(target); setError(''); setOutcome(''); window.scrollTo({top: 0});}, []);
  const reset = useCallback(() => {
    authEpoch.current++; setAuthenticated(false); setData(null); setPreview(null); setMenu(false);
    setActiveJob(null); setBusy(false); setOutcome(''); setPreviewError(''); setPage('overview'); setSelectedSiteId('default');
  }, []);
  useEffect(() => {
    void session().then(setAuthenticated).catch(error => {setError(message(error)); setAuthenticated(false);});
    const expired = () => {reset(); setError('登录已失效，请重新登录。');};
    window.addEventListener('proxyhub:unauthorized', expired);
    return () => window.removeEventListener('proxyhub:unauthorized', expired);
  }, [reset]);
  useEffect(() => {document.documentElement.dataset.theme = theme; try {localStorage.setItem('proxyhub-theme', theme);} catch { /* Appearance remains in memory. */ }}, [theme]);
  useEffect(() => {if (!toast) return; const timer = window.setTimeout(() => setToast(''), 4500); return () => window.clearTimeout(timer);}, [toast]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {if (authenticated && !preview && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {event.preventDefault(); setMenu(value => !value);}};
    window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler);
  }, [authenticated, preview]);
  async function refresh() {
    const epoch = authEpoch.current; setLoading(true); setError('');
    try {const result = await operation<Overview>('overview'); if (epoch === authEpoch.current) setData(result);}
    catch (error) {if (epoch === authEpoch.current) setError(message(error));}
    finally {setLoading(false);}
  }
  useEffect(() => {if (authenticated) void refresh();}, [authenticated]);
  useEffect(() => {
    if (data && !data.websites.some(site => site.id === selectedSiteId)) setSelectedSiteId('default');
  }, [data, selectedSiteId]);
  async function runJob<T>(name: string, payload: JsonObject): Promise<T> {
    const epoch = authEpoch.current;
    const result = await operation<{job_id: string}>(name, payload);
    const output = await awaitJob<T>(result.job_id, job => {if (epoch === authEpoch.current) setActiveJob(job);});
    if (epoch !== authEpoch.current) throw new ApiError('session_changed', '登录状态已变化，请重新读取任务结果。');
    return output;
  }
  async function prepare(kind: string, values: JsonObject) {
    if (!data || busy) return;
    setBusy(true); setError(''); setOutcome(''); setPreviewError(''); setActiveJob(null);
    try {setPreview(await runJob<Preview>('plan.create', {kind, base_revision: data.revision, values}));}
    catch (error) {setError(message(error));} finally {setBusy(false);}
  }
  async function apply() {
    if (!preview || busy) return;
    setBusy(true); setPreviewError('');
    const kind = preview.kind;
    try {
      await runJob<JsonObject>('plan.apply', {plan_id: preview.plan_id}); setPreview(null);
      const text = kind === 'ssh.append' ? '公钥操作已完成。请另开 SSH 会话验证登录，当前验证状态仍为待验证。' : '变更已应用，已读取最新部署状态。';
      setOutcome(text); notify('变更已应用'); await refresh();
    } catch (error) {setPreviewError(message(error));} finally {setBusy(false);}
  }
  async function recover() {
    if (busy) return; setBusy(true); setError('');
    try {await runJob<JsonObject>('deployment.recover', {}); setOutcome('部署恢复检查已完成，请查看任务记录与服务状态。'); await refresh();}
    catch (error) {setError(message(error));} finally {setBusy(false);}
  }
  async function signOut() {
    setLoggingOut(true); reset(); setError('');
    try {await logout();}
    catch {setError('本地敏感内容已清除；服务器会话注销未确认，可能仍有效。请恢复连接后重新登录并退出。');}
    finally {setLoggingOut(false);}
  }
  if (authenticated === null) return <div className="boot-screen" role="status"><img src="/favicon.svg" alt="" /><p>正在连接 ProxyHub…</p></div>;
  if (!authenticated) return <Login initialError={error} blocked={loggingOut} onLogin={() => {authEpoch.current++; setError(''); setAuthenticated(true);}} />;
  const formProps = data ? {data, prepare, busy, notify} : null;
  return <>
    <Shell page={page} navigate={navigate} data={data} theme={theme} toggleTheme={() => setTheme(theme === 'light' ? 'dark' : 'light')} command={() => setMenu(true)} refresh={() => void refresh()} logout={() => void signOut()} notify={notify} busy={busy || loading}>
      {error && <Notice tone="error"><span>{error}</span><button className="text-button" onClick={() => void refresh()} disabled={busy || loading}>重新读取</button></Notice>}
      {outcome && <Notice tone="success">{outcome}</Notice>}
      {busy && <div className="operation-progress" role="status"><span className="spinner" /><span>{activeJob ? `${activeJob.operation} · 正在处理` : '正在提交任务…'}</span>{activeJob && <button className="text-button" onClick={() => navigate('jobs')}>查看任务</button>}</div>}
      {loading && !data && <div className="loading-state" role="status"><span className="spinner" /><p>正在读取服务器与部署状态…</p></div>}
      {data && formProps && <div key={`${page}-${data.revision}`} className="page-body">
        {page === 'overview' && <OverviewPage data={data} navigate={navigate} />}
        {page === 'server' && <ServerPage {...formProps} />}
        {page === 'site' && <SitePage key={selectedSiteId} {...formProps} selectedSiteId={selectedSiteId} selectSite={setSelectedSiteId} />}
        {page === 'proxy' && <ProxyPage {...formProps} />}
        {page === 'subscription' && <SubscriptionPage {...formProps} />}
        {page === 'jobs' && <JobsPage activeId={activeJob?.id} activeStatus={activeJob?.status} recover={() => void recover()} busy={busy} />}
        {page === 'settings' && <SettingsPage data={data} theme={theme} setTheme={setTheme} notify={notify} />}
      </div>}
      {!data && !loading && !error && <Notice>尚无可读取的部署。请通过 CLI 完成初始部署后刷新。</Notice>}
      {activeJob && !busy && page !== 'jobs' && <button className="last-job" onClick={() => navigate('jobs')}><span>最近任务</span><Status value={activeJob.status} /><Icon name="arrow" /></button>}
    </Shell>
    {preview && <PreviewDrawer preview={preview} busy={busy} apply={() => void apply()} cancel={() => {setPreview(null); setPreviewError(''); notify('已关闭预览，未执行变更');}} error={previewError} />}
    {menu && !preview && <CommandMenu close={() => setMenu(false)} navigate={navigate} refresh={() => void refresh()} />}
    {toast && <div className="toast" role="status"><Icon name="check" /><span>{toast}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setToast('')}><Icon name="close" /></button></div>}
  </>;
}
