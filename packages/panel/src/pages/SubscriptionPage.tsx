import { useEffect, useRef, useState } from 'react';
import { message, operation } from '../api';
import type { FormProps } from '../types';
import { copyText, Details, Field, Notice, Section } from '../components/common';
import { Icon } from '../components/Icon';
import { resolveTimestamp, timestampView } from '../timestamp';

export function SubscriptionPage({data, prepare, busy, notify}: FormProps) {
  const sub = data.subscription;
  const [name, setName] = useState(sub.name);
  const [interval, setInterval] = useState(String(sub.update_interval_hours));
  const [manual, setManual] = useState(sub.userinfo.upload !== undefined);
  const [usage, setUsage] = useState({upload: String(sub.userinfo.upload ?? 0), download: String(sub.userinfo.download ?? 0), total: String(sub.userinfo.total ?? 0)});
  const expireView = timestampView(sub.userinfo.expire);
  const resetView = timestampView(sub.reset_at);
  const [expire, setExpire] = useState(expireView.value);
  const [expireEdited, setExpireEdited] = useState(false);
  const [resetDay, setResetDay] = useState(sub.reset_day === null ? '' : String(sub.reset_day));
  const [resetAt, setResetAt] = useState(resetView.value);
  const [resetEdited, setResetEdited] = useState(false);
  const [overrides, setOverrides] = useState(JSON.stringify(sub.overrides, null, 2));
  const [error, setError] = useState('');
  const [revealing, setRevealing] = useState(false);
  const [links, setLinks] = useState<{mihomo: string; raw: string} | null>(null);
  const [yaml, setYaml] = useState<string | null>(null);
  const revealGeneration = useRef(0);
  useEffect(() => () => {revealGeneration.current++;}, []);
  function hide() {
    revealGeneration.current++; setLinks(null); setYaml(null); setRevealing(false);
  }
  async function reveal(format: 'links' | 'yaml') {
    const generation = ++revealGeneration.current;
    setRevealing(true); setError('');
    try {
      const result = await operation<{links?: {mihomo: string; raw: string}; yaml?: string}>('subscription.reveal', {format});
      if (generation === revealGeneration.current) {
        if (format === 'links') setLinks(result.links ?? null); else setYaml(result.yaml ?? null);
      }
    } catch (error) {if (generation === revealGeneration.current) setError(message(error));}
    finally {if (generation === revealGeneration.current) setRevealing(false);}
  }
  function submit() {
    setError('');
    try {
      const parsed: unknown = JSON.parse(overrides);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('配置覆盖必须是一个 JSON 对象。');
      const userinfo: Record<string, number> = {};
      if (manual) for (const key of ['upload', 'download', 'total'] as const) {
        const number = Number(usage[key]);
        if (!Number.isSafeInteger(number) || number < 0) throw new Error('流量值必须是非负安全整数，单位为字节。');
        userinfo[key] = number;
      }
      const expiration = resolveTimestamp(sub.userinfo.expire, expire, expireEdited, '到期时间');
      if (expiration !== null) userinfo.expire = expiration;
      const reset = resolveTimestamp(sub.reset_at, resetAt, resetEdited, '下次重置时间');
      void prepare('subscription.update', {name, update_interval_hours: Number(interval), userinfo, reset_day: resetDay ? Number(resetDay) : null, reset_at: reset, overrides: parsed});
    } catch (error) {setError(error instanceof SyntaxError ? '配置覆盖不是有效 JSON，请检查引号、逗号与括号。' : message(error));}
  }
  return <>
    {error && <Notice tone="error">{error}</Notice>}
    <Section title="客户端配置与显示信息" description="配置刷新会保留个人覆盖。订阅发布只更新发布服务，不重启代理与网站。">
      <form className="form-stack" onSubmit={event => {event.preventDefault(); submit();}}>
        <div className="form-grid"><Field label="订阅名称"><input value={name} onChange={event => setName(event.target.value)} required maxLength={120} disabled={busy} /></Field><Field label="建议刷新间隔（小时）" hint="向支持的客户端提供刷新建议。"><input type="number" min={1} max={168} step={1} value={interval} required onChange={event => setInterval(event.target.value)} disabled={busy} /></Field></div>
        <div className="form-subsection"><h3>流量与到期信息</h3><p className="muted">以下为手工记录，不代表实时计量。刷新间隔、到期时间与流量重置各自独立。</p>
          <label className="checkbox-label"><input type="checkbox" checked={manual} onChange={event => setManual(event.target.checked)} disabled={busy} /><span>向客户端显示手工流量信息</span></label>
          {manual && <div className="form-grid three-fields">{([['upload', '已上传（字节）'], ['download', '已下载（字节）'], ['total', '总额度（字节）']] as const).map(([key, label]) => <Field key={key} label={label}><input type="number" min={0} max={Number.MAX_SAFE_INTEGER} step={1} value={usage[key]} required disabled={busy} onChange={event => setUsage({...usage, [key]: event.target.value})} /></Field>)}</div>}
          <Field label="到期时间（浏览器本地时区）" hint="精确到秒；未编辑时保留原始值。到期字段仅用于客户端显示，不自动撤销节点凭据。"><input type="datetime-local" step={1} max="9999-12-31T23:59:59" value={expire} onChange={event => {setExpire(event.target.value); setExpireEdited(true);}} disabled={busy} /></Field>
          {expireView.warning && !expireEdited && <Notice tone="warning">{expireView.warning}</Notice>}
          {(sub.userinfo.expire !== undefined || expire) && <div><button className="text-button" type="button" disabled={busy} onClick={() => {setExpire(''); setExpireEdited(true);}}>清除到期时间</button></div>}
        </div>
        <div className="form-subsection"><h3>重置记录</h3><p className="muted">当前作为管理记录保存，不自动计量或执行额度重置，也不会写入到期字段。</p><div className="form-grid"><Field label="每月重置日" hint="1–31，可留空。"><input type="number" min={1} max={31} step={1} value={resetDay} onChange={event => setResetDay(event.target.value)} disabled={busy} /></Field><Field label="下次重置时间（浏览器本地时区）" hint="精确到秒；未编辑时保留原始值。"><input type="datetime-local" step={1} max="9999-12-31T23:59:59" value={resetAt} onChange={event => {setResetAt(event.target.value); setResetEdited(true);}} disabled={busy} /></Field></div>{resetView.warning && !resetEdited && <Notice tone="warning">{resetView.warning}</Notice>}{(sub.reset_at !== null || resetAt) && <div><button className="text-button" type="button" disabled={busy} onClick={() => {setResetAt(''); setResetEdited(true);}}>清除下次重置时间</button></div>}</div>
        <Field label="客户端配置覆盖（JSON）" hint="可用字段：mixed-port、log-level、mode、ipv6、allow-lan（只能为 false）。节点、策略组与规则由系统生成；冲突会在预览时报告。"><textarea className="code-input" value={overrides} onChange={event => setOverrides(event.target.value)} disabled={busy} rows={10} spellCheck={false} maxLength={32768} required /></Field>
        <details className="advanced"><summary>配置覆盖的允许值与示例</summary><Details rows={[
          ['mixed-port', '1–65535 的整数；默认 17890'],
          ['log-level', <code>silent | error | warning | info | debug</code>],
          ['mode', <code>rule | global | direct</code>],
          ['ipv6', <code>true | false</code>],
          ['allow-lan', <code>false</code>],
        ]} /><pre className="yaml-preview" tabIndex={0}>{'{\n  "mixed-port": 17890,\n  "log-level": "warning",\n  "mode": "rule",\n  "ipv6": false,\n  "allow-lan": false\n}'}</pre></details>
        <div className="form-actions"><button className="button primary" disabled={busy}>预览订阅变更</button></div>
      </form>
    </Section>
    <Section title="客户端访问" description="访问链接与最终 YAML 包含连接凭据。仅在需要时显示，离开此页面后隐藏。">
      <Details rows={[
        ['发布域名', <code>{sub.domain}</code>], ['格式', 'Mihomo YAML / 原始节点链接'], ['流量来源', sub.traffic_source === 'manual' ? '手工填写' : sub.traffic_source],
      ]} />
      <div className="form-actions"><button className="button secondary" disabled={revealing || busy} onClick={() => void reveal('links')}><Icon name="subscription" />显示订阅链接</button><button className="button secondary" disabled={revealing || busy} onClick={() => void reveal('yaml')}><Icon name="terminal" />查看最终 YAML</button>{(links || yaml !== null || revealing) && <button className="text-button" onClick={hide}>隐藏凭据</button>}</div>
      {revealing && <p role="status" className="muted">正在读取当前发布内容…</p>}
      {links && <div className="secret-content">{([['mihomo', 'Mihomo 订阅链接'], ['raw', '原始节点链接']] as const).map(([key, label]) => <div className="reveal-item" key={key}><div><strong>{label}</strong><button className="text-button" onClick={() => void copyText(links[key], notify)}><Icon name="copy" />复制</button></div><code>{links[key]}</code></div>)}</div>}
      {yaml !== null && <div className="secret-content"><div className="reveal-heading"><strong>当前发布 YAML</strong><button className="text-button" onClick={() => void copyText(yaml, notify)}><Icon name="copy" />复制 YAML</button></div><pre className="yaml-preview" tabIndex={0}>{yaml}</pre></div>}
    </Section>
  </>;
}
