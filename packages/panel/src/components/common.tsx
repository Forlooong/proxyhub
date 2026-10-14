import { useId, type ReactNode } from 'react';
import { Icon } from './Icon';

export function Field({label, hint, children, className = ''}: {label: string; hint?: string; children: ReactNode; className?: string}) {
  const id = useId();
  return <label className={`field ${className}`}><span className="field-label">{label}</span>{children}{hint && <span className="field-hint" id={id}>{hint}</span>}</label>;
}
export function Section({title, description, children, action}: {title: string; description?: string; children: ReactNode; action?: ReactNode}) {
  return <section className="section"><div className="section-head"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>{action}</div>{children}</section>;
}
export function Details({rows}: {rows: [string, ReactNode][]}) {
  return <dl className="details">{rows.map(([label, value]) => <div className="detail-row" key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}
export function Notice({children, tone = 'info'}: {children: ReactNode; tone?: 'info' | 'warning' | 'error' | 'success'}) {
  return <div className={`notice notice-${tone}`} role={tone === 'error' ? 'alert' : undefined}>{children}</div>;
}
export function Status({value}: {value: string}) {
  if (value.length > 32) return <span className="verification-note">{value}</span>;
  const good = ['active', 'running', 'succeeded', 'applied', 'verified', 'healthy'].includes(value);
  const failed = ['failed', 'inactive', 'interrupted', 'error'].includes(value);
  const labels: Record<string, string> = {active: '运行中', running: '处理中', succeeded: '已完成', applied: '已应用', verified: '已验证', healthy: '正常', failed: '失败', inactive: '未运行', interrupted: '已中断', error: '异常', queued: '排队中', unverified: '待验证', unknown: '未知', 'not_checked': '未检查'};
  return <span className={`status ${good ? 'status-good' : failed ? 'status-bad' : 'status-neutral'}`}><span className="status-dot" />{labels[value] ?? value}</span>;
}
export async function copyText(text: string, notify: (message: string) => void) {
  try { await navigator.clipboard.writeText(text); notify('已复制到剪贴板'); }
  catch { notify('浏览器未允许复制，请手动选择内容。'); }
}
export function Command({text, notify}: {text: string; notify: (message: string) => void}) {
  return <div className="command"><Icon name="terminal" /><code>{text}</code><button className="icon-button" aria-label="复制命令" title="复制命令" onClick={() => void copyText(text, notify)}><Icon name="copy" /></button></div>;
}
export function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未设置';
  return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
}
export function date(value?: string | number | null): string {
  if (value === undefined || value === null || value === '') return '—';
  const parsed = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString('zh-CN', {hour12: false});
}
export function formatBytes(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes)) return '未知';
  if (bytes < 1024) return `${bytes} B`;
  const index = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** index).toFixed(1)} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][index]}`;
}
