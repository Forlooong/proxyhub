import type { Preview } from '../types';
import { Dialog } from './Dialog';
import { display, Notice } from './common';
import { Icon } from './Icon';

export function PreviewDrawer({preview, busy, apply, cancel, error}: {preview: Preview; busy: boolean; apply: () => void; cancel: () => void; error: string}) {
  return <Dialog title="检查变更" onClose={cancel} locked={busy} className="preview-drawer">
    <div className="drawer-content">
      <span className="revision">基于版本 {preview.base_revision}</span>
      <h3 className="drawer-summary">{preview.summary}</h3>
      <p className="muted">确认后才会执行。取消关闭此预览，保留当前服务。</p>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="changes">{preview.changes.map((change, index) => <section className="change" key={`${change.label}-${index}`}><h4>{change.label}</h4><div className="change-value"><span>当前</span><pre>{display(change.before)}</pre></div><div className="change-value change-after"><span>变更后</span><pre>{display(change.after)}</pre></div></section>)}</div>
      {preview.impact.length > 0 && <section className="drawer-section"><h4>影响范围</h4><ul>{preview.impact.map((item, index) => <li key={index}>{item}</li>)}</ul></section>}
      {preview.warnings.length > 0 && <Notice tone="warning"><strong>执行前留意</strong><ul>{preview.warnings.map((item, index) => <li key={index}>{item}</li>)}</ul></Notice>}
    </div>
    <div className="drawer-footer"><button className="button secondary" disabled={busy} onClick={cancel}>取消</button><button className="button primary" disabled={busy} onClick={apply}><Icon name="check" />{busy ? '正在应用…' : '确认应用'}</button></div>
  </Dialog>;
}
