import { useEffect, useState } from 'react';
import { message, operation } from '../api';
import type { Preview } from '../types';
import { Dialog } from './Dialog';
import { display, formatBytes, Notice } from './common';
import { Icon } from './Icon';

export function PreviewDrawer({preview, busy, apply, cancel, error}: {preview: Preview; busy: boolean; apply: () => void; cancel: () => void; error: string}) {
  const [contentRequest, setContentRequest] = useState<{planId: string} | null>(null);
  const [content, setContent] = useState<{planId: string; html: string; file_count: number; total_bytes: number} | null>(null);
  const [contentError, setContentError] = useState('');
  const [loadingContent, setLoadingContent] = useState(false);
  const contentPlan = preview.kind.startsWith('website.content.');
  useEffect(() => {
    let active = true;
    setContent(null); setContentError(''); setLoadingContent(false);
    if (contentPlan && contentRequest?.planId === preview.plan_id) {
      setLoadingContent(true);
      void operation<{html: string; file_count: number; total_bytes: number}>('website.content.preview', {plan_id: preview.plan_id}).then(result => {
        if (active) setContent({...result, planId: preview.plan_id});
      }).catch(error => {if (active) setContentError(message(error));}).finally(() => {if (active) setLoadingContent(false);});
    }
    return () => {active = false;};
  }, [preview.plan_id, contentPlan, contentRequest]);
  return <Dialog title="检查变更" onClose={cancel} locked={busy} className="preview-drawer">
    <div className="drawer-content">
      <span className="revision">基于版本 {preview.base_revision}</span>
      <h3 className="drawer-summary">{preview.summary}</h3>
      <p className="muted">确认后才会执行。取消关闭此预览，保留当前服务。</p>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="changes">{preview.changes.map((change, index) => <section className="change" key={`${change.label}-${index}`}><h4>{change.label}</h4><div className="change-value"><span>当前</span><pre>{display(change.before)}</pre></div><div className="change-value change-after"><span>变更后</span><pre>{display(change.after)}</pre></div></section>)}</div>
      {contentPlan && <section className="drawer-section"><h4>网站页面预览</h4>
        <Notice>预览禁用脚本和外部资源；依赖 JavaScript 或 CSS url() 的页面可能无法完整呈现。</Notice>
        <div className="form-actions"><button className="button secondary" disabled={busy || loadingContent} onClick={() => setContentRequest({planId: preview.plan_id})}>{loadingContent ? '正在生成页面预览…' : '加载页面预览'}</button></div>
        {contentError && <Notice tone="error">{contentError}</Notice>}
        {content?.planId === preview.plan_id && <><p className="muted">{content.file_count} 个文件 · {formatBytes(content.total_bytes)}</p><iframe title="网站内容安全预览" sandbox="" referrerPolicy="no-referrer" srcDoc={content.html} width="100%" height="360" /></>}
      </section>}
      {preview.impact.length > 0 && <section className="drawer-section"><h4>影响范围</h4><ul>{preview.impact.map((item, index) => <li key={index}>{item}</li>)}</ul></section>}
      {preview.warnings.length > 0 && <Notice tone="warning"><strong>执行前留意</strong><ul>{preview.warnings.map((item, index) => <li key={index}>{item}</li>)}</ul></Notice>}
    </div>
    <div className="drawer-footer"><button className="button secondary" disabled={busy} onClick={cancel}>取消</button><button className="button primary" disabled={busy} onClick={apply}><Icon name="check" />{busy ? '正在应用…' : '确认应用'}</button></div>
  </Dialog>;
}
