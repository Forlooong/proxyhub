import { useEffect, useRef, useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import { message, operation } from '../api';
import type { FormProps, SubscriptionProfile } from '../types';
import { copyText, Details, Field, Notice, Section } from '../components/common';
import { Icon } from '../components/Icon';
import { resolveTimestamp, timestampView } from '../timestamp';

export function SubscriptionPage({data, prepare, busy, notify}: FormProps) {
  const [selectedId, setSelectedId] = useState('default');
  const [name, setName] = useState('');
  const [selectedNodes, setSelectedNodes] = useState<string[]>([]);
  const sub = data.subscriptions.profiles.find(item => item.id === selectedId) ?? data.subscriptions.profiles[0];
  if (!sub) return <Notice tone="error">未读取到订阅配置，请刷新部署状态后重试。</Notice>;
  return <>
    <Section title="多订阅" description="每份订阅引用已有节点，拥有独立链接、名称、覆盖和手工元数据；共享节点不会新增核心或监听进程。">
      <Field label="当前订阅"><select value={sub.id} disabled={busy} onChange={event => setSelectedId(event.target.value)}>{data.subscriptions.profiles.map(item => <option key={item.id} value={item.id}>{item.name} · {item.enabled ? '已启用' : '已停用'} · {item.id}</option>)}</select></Field>
      <details className="advanced"><summary>创建订阅</summary>
        <form className="form-stack" onSubmit={event => {event.preventDefault(); void prepare('subscription.create', {name, node_ids: selectedNodes});}}>
          <Field label="新订阅名称"><input required maxLength={200} value={name} disabled={busy} onChange={event => setName(event.target.value)} /></Field>
          <fieldset className="subscription-nodes"><legend>引用节点（至少一个）</legend>{data.subscriptions.nodes.map(node => <label className="checkbox-label" key={node.id}><input type="checkbox" checked={selectedNodes.includes(node.id)} disabled={busy || !node.enabled} onChange={event => setSelectedNodes(event.target.checked ? [...selectedNodes, node.id] : selectedNodes.filter(id => id !== node.id))} /><span>{node.name} · {node.protocol.toUpperCase()}{!node.enabled && ' · 已停用'}</span></label>)}</fieldset>
          <div className="form-actions"><button className="button secondary" disabled={busy || selectedNodes.length === 0}>预览创建订阅</button></div>
        </form>
      </details>
    </Section>
    <SubscriptionEditor key={sub.id} data={data} prepare={prepare} busy={busy} notify={notify} sub={sub} />
  </>;
}

function SubscriptionEditor({data, prepare, busy, notify, sub}: FormProps & {sub: SubscriptionProfile}) {
  const [name, setName] = useState(sub.name);
  const [host, setHost] = useState(sub.host);
  const [prefix, setPrefix] = useState(sub.path_prefix);
  const [allNodes, setAllNodes] = useState(sub.node_ids === null);
  const [nodeIds, setNodeIds] = useState(sub.node_ids ?? data.subscriptions.nodes.filter(node => node.enabled).map(node => node.id));
  const [formats, setFormats] = useState(sub.formats);
  const [copyName, setCopyName] = useState(sub.name + ' 副本');
  const [aliasHost, setAliasHost] = useState(sub.host);
  const [aliasPrefix, setAliasPrefix] = useState('');
  const hosts = [...new Set([data.subscription.domain, ...data.subscriptions.profiles.flatMap(item => [item.host, ...item.aliases.map(alias => alias.host)]), ...data.websites.filter(site => site.enabled).flatMap(site => site.domains)])];
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
  const [links, setLinks] = useState<Record<string, string> | null>(null);
  const [imports, setImports] = useState<Record<string, string> | null>(null);
  const [yaml, setYaml] = useState<string | null>(null);
  const revealGeneration = useRef(0);
  useEffect(() => () => {revealGeneration.current++;}, []);
  function hide() {
    revealGeneration.current++; setLinks(null); setImports(null); setYaml(null); setRevealing(false);
  }
  async function reveal(format: 'links' | 'yaml') {
    const generation = ++revealGeneration.current;
    setRevealing(true); setError('');
    try {
      const result = await operation<{links?: Record<string, string>; aliases?: {id: string; links: Record<string, string>}[]; yaml?: string; imports?: Record<string, string>}>('subscription.reveal', {format, subscription_id: sub.id});
      if (generation === revealGeneration.current) {
        if (format === 'links') {
          setLinks(result.links ? {...result.links, ...Object.fromEntries((result.aliases ?? []).flatMap(alias => Object.entries(alias.links).map(([kind, url]) => [`别名 ${alias.id} · ${kind}`, url])))} : null);
          setImports(result.imports ?? null);
        } else setYaml(result.yaml ?? null);
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
      if (!formats.length || (!allNodes && !nodeIds.length)) throw new Error('至少选择一种格式和一个节点。');
      void prepare('subscription.update', {subscription_id: sub.id, name, host, path_prefix: prefix,
        node_ids: allNodes ? null : nodeIds, formats, update_interval_hours: Number(interval), userinfo,
        reset_day: resetDay ? Number(resetDay) : null, reset_at: reset, overrides: parsed});
    } catch (error) {setError(error instanceof SyntaxError ? '配置覆盖不是有效 JSON，请检查引号、逗号与括号。' : message(error));}
  }
  return <>
    {error && <Notice tone="error">{error}</Notice>}
    <Section title="订阅生命周期" description="复制生成独立身份与令牌；停用保留配置。令牌轮换撤销所有别名下的旧令牌，不修改节点凭据。">
      <Details rows={[
        ['稳定编号', <code>{sub.id}</code>], ['启用状态', sub.enabled ? '已启用' : '已停用'],
        ['下载安全状态', sub.access_status === 'active' ? '可用' : sub.access_status === 'blocked' ? '已撤销或路径已退役' : '未确认'],
      ]} />
      {sub.access_status !== 'active' && <Notice tone="warning">{sub.access_status === 'blocked' ? '已撤销的令牌不会因恢复旧配置而复活。请轮换令牌；已退役的主路径还需改用有效路径。' : '下载安全状态未确认，暂不显示凭据。请检查发布和恢复状态。'}</Notice>}
      <div className="form-actions"><button className="button secondary" disabled={busy} onClick={() => void prepare(sub.enabled ? 'subscription.disable' : 'subscription.enable', {subscription_id: sub.id})}>预览{sub.enabled ? '停用' : '启用'}</button><button className="button secondary" disabled={busy} onClick={() => void prepare('subscription.token.rotate', {subscription_id: sub.id})}>预览轮换下载令牌</button></div>
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); void prepare('subscription.copy', {subscription_id: sub.id, name: copyName});}}><Field label="副本名称"><input value={copyName} onChange={event => setCopyName(event.target.value)} required maxLength={200} disabled={busy} /></Field><div><button className="button secondary" disabled={busy}>预览复制订阅</button></div></form>
      <Notice>旧下载令牌撤销后，已下载配置中的节点身份仍可连接。节点凭据轮换是独立操作，不能由订阅令牌轮换代替。</Notice>
    </Section>
    <Section title="客户端配置与显示信息" description="配置刷新会保留个人覆盖。订阅发布只更新发布服务，不重启代理与网站。">
      <form className="form-stack" onSubmit={event => {event.preventDefault(); submit();}}>
        <div className="form-grid"><Field label="订阅名称"><input value={name} onChange={event => setName(event.target.value)} required maxLength={200} disabled={busy} /></Field><Field label="建议刷新间隔（小时）" hint="向支持的客户端提供刷新建议。"><input type="number" min={1} max={168} step={1} value={interval} required onChange={event => setInterval(event.target.value)} disabled={busy} /></Field></div>
        <div className="form-grid"><Field label="发布域名" hint="仅可使用已有受管 HTTPS 域名；变更后保留旧别名。"><select value={host} disabled={busy} onChange={event => setHost(event.target.value)}>{hosts.map(domain => <option key={domain}>{domain}</option>)}</select></Field><Field label="发布路径前缀" hint="使用 /sub 或 /family 这类固定路径，不包含令牌。"><input value={prefix} required maxLength={128} disabled={busy} onChange={event => setPrefix(event.target.value)} /></Field></div>
        <fieldset className="subscription-nodes"><legend>引用已有节点</legend>{sub.id === 'default' && <label className="checkbox-label"><input type="checkbox" checked={allNodes} disabled={busy} onChange={event => setAllNodes(event.target.checked)} /><span>沿用原默认行为：包含全部已启用节点</span></label>}{data.subscriptions.nodes.map(node => <label className="checkbox-label" key={node.id}><input type="checkbox" checked={allNodes ? node.enabled : nodeIds.includes(node.id)} disabled={busy || allNodes} onChange={event => setNodeIds(event.target.checked ? [...nodeIds, node.id] : nodeIds.filter(id => id !== node.id))} /><span>{node.name} · {node.protocol.toUpperCase()}{!node.enabled && ' · 已停用（保留引用）'}</span></label>)}</fieldset>
        <fieldset className="subscription-nodes"><legend>下载格式</legend>{(['yaml', 'raw'] as const).map(format => <label className="checkbox-label" key={format}><input type="checkbox" checked={formats.includes(format)} disabled={busy} onChange={event => setFormats(event.target.checked ? [...formats, format] : formats.filter(item => item !== format))} /><span>{format === 'yaml' ? 'Mihomo YAML' : '原始节点链接 / Base64'}</span></label>)}</fieldset>
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
        ['发布域名', <code>{sub.host}</code>], ['发布路径', <code>{sub.path_prefix}</code>], ['格式', sub.formats.join(' / ')], ['流量来源', '手工填写'],
      ]} />
      <div className="form-actions"><button className="button secondary" disabled={revealing || busy || !sub.enabled || sub.access_status !== 'active'} onClick={() => void reveal('links')}><Icon name="subscription" />显示订阅链接</button><button className="button secondary" disabled={revealing || busy || !sub.enabled || sub.access_status !== 'active' || !sub.formats.includes('yaml')} onClick={() => void reveal('yaml')}><Icon name="terminal" />查看最终 YAML</button>{(links || yaml !== null || revealing) && <button className="text-button" onClick={hide}>隐藏凭据</button>}</div>
      {revealing && <p role="status" className="muted">正在读取当前发布内容…</p>}
      {links && <div className="secret-content">{Object.entries(links).map(([key, url]) => <div className="reveal-item" key={key}><div><strong>{key === 'mihomo' ? 'Mihomo 订阅链接' : key === 'raw' ? '原始节点链接' : key}</strong><button className="text-button" onClick={() => void copyText(url, notify)}><Icon name="copy" />复制</button></div><code>{url}</code><details className="advanced"><summary>显示含凭据的二维码</summary><QRCodeSVG className="subscription-qr" value={url} size={192} marginSize={4} bgColor="#fff" fgColor="#000" title="订阅链接二维码，含下载凭据" /></details></div>)}{imports && Object.entries(imports).map(([client, uri]) => <a className="button secondary" key={client} href={uri}>导入 {client}</a>)}<p className="muted">可复制 YAML 链接到客户端订阅导入。Clash Verge Rev 2.5.8 的 GUI 和元数据显示以本轮实际验收记录为准，未完成前不能视为已验证。</p></div>}
      {yaml !== null && <div className="secret-content"><div className="reveal-heading"><strong>当前发布 YAML</strong><button className="text-button" onClick={() => void copyText(yaml, notify)}><Icon name="copy" />复制 YAML</button></div><pre className="yaml-preview" tabIndex={0}>{yaml}</pre></div>}
    </Section>
    <Section title="保留别名" description="域名或路径变更会保留旧入口。退役永久关闭对应域名与路径的新下载；恢复旧配置不会使其复活。">
      {sub.aliases.length === 0 && <p className="muted">暂无保留别名。</p>}
      {sub.aliases.map(alias => <div className="job-item" key={alias.id}><Details rows={[
        ['域名与路径', <code>{alias.host}{alias.path_prefix}</code>], ['编号', <code>{alias.id}</code>], ['安全状态', alias.access_status === 'active' ? '可用' : alias.access_status === 'blocked' ? '已撤销或退役' : '未确认'],
      ]} /><button className="button secondary" disabled={busy} onClick={() => void prepare('subscription.alias.retire', {subscription_id: sub.id, alias_id: alias.id})}>预览退役此别名</button></div>)}
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); void prepare('subscription.alias.add', {subscription_id: sub.id, host: aliasHost, path_prefix: aliasPrefix});}}><div className="form-grid"><Field label="别名域名"><select value={aliasHost} disabled={busy} onChange={event => setAliasHost(event.target.value)}>{hosts.map(domain => <option key={domain}>{domain}</option>)}</select></Field><Field label="别名路径前缀"><input value={aliasPrefix} required maxLength={128} disabled={busy} onChange={event => setAliasPrefix(event.target.value)} /></Field></div><div><button className="button secondary" disabled={busy}>预览添加别名</button></div></form>
    </Section>
  </>;
}
