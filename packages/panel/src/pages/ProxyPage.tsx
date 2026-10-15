import { useState } from 'react';
import type { FormProps } from '../types';
import { Details, Field, Notice, Section, Status } from '../components/common';

export function ProxyPage({data, prepare, busy}: FormProps) {
  const [name, setName] = useState(data.proxy.name);
  const [address, setAddress] = useState(data.proxy.public_address);
  const sites = data.websites.filter(site => site.enabled && site.domains.length > 0);
  const primary = data.reality.endpoints.find(endpoint => endpoint.id === data.reality.primary_endpoint_id)!;
  const [targetType, setTargetType] = useState<'site' | 'external'>('site');
  const [siteId, setSiteId] = useState(primary.site_id ?? sites[0]?.id ?? '');
  const site = sites.find(item => item.id === siteId);
  const [siteSni, setSiteSni] = useState(primary.site_id ? primary.sni : sites[0]?.domains[0] ?? '');
  const [externalAddress, setExternalAddress] = useState('');
  const [externalSni, setExternalSni] = useState('');
  const selectedSni = site?.domains.includes(siteSni) ? siteSni : site?.domains[0] ?? '';
  return <>
    <Section title="REALITY 节点" description="管理当前单节点的客户端显示名称与连接地址。">
      <Details rows={[
        ['协议', data.proxy.protocol], ['公网端口', data.proxy.port], ['订阅使用的 SNI', <code>{data.proxy.sni}</code>], ['当前握手目标', <code>{data.proxy.target}</code>], ['验证记录', <Status value={data.proxy.verification} />],
      ]} />
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); void prepare('proxy.update', {node_name: name, public_address: address});}}>
        <Field label="节点名称"><input value={name} required maxLength={120} disabled={busy} onChange={event => setName(event.target.value)} /></Field>
        <Field label="客户端连接地址" hint="填写公网 IP 或可解析到本机的域名，不包含协议或端口。"><input className="code-input" value={address} required maxLength={253} disabled={busy} onChange={event => setAddress(event.target.value)} /></Field>
        <Notice>节点变更会更新关联订阅。应用后请让客户端刷新订阅，并完成一次真实代理请求。</Notice>
        <div className="form-actions"><button className="button primary" disabled={busy || (name === data.proxy.name && address === data.proxy.public_address)}>预览节点变更</button></div>
      </form>
    </Section>
    <Section title="REALITY 入口与迁移" description="新目标使用独立入口；旧入口保留，直至单独确认退役。订阅只发布当前主入口。">
      <div className="job-list">{data.reality.endpoints.map(endpoint => {
        const targetSite = data.websites.find(item => item.id === endpoint.site_id);
        return <div className="job-item" key={endpoint.id}>
          <div className="form-actions"><strong><code>{endpoint.sni}</code></strong><span className={`status ${endpoint.primary ? 'status-good' : 'status-neutral'}`}>{endpoint.primary ? '当前主入口' : '已保留'}</span></div>
          <Details rows={[
            ['入口编号', <code>{endpoint.id}</code>],
            ['内部监听', <code>{endpoint.listener_id} · 127.0.0.1:{endpoint.listen_port}</code>],
            ['握手目标', <code>{endpoint.address}:{endpoint.port}</code>],
            ['目标类型', endpoint.target_type === 'site' ? '本机受管网站' : '外部 TLS 网站'],
            ['依赖关系', targetSite ? `${targetSite.title} · 网站 ${targetSite.id} · 证书 ${targetSite.certificate_id ?? '未绑定'}` : '外部目标的 DNS、证书与 TLS 能力'],
            ['订阅', endpoint.primary ? '刷新订阅后使用此入口' : '原有客户端可继续使用；切为主入口后重新发布到订阅'],
          ]} />
          <div className="form-actions"><button className="button secondary" disabled={busy || endpoint.primary} onClick={() => void prepare('reality.select', {endpoint_id: endpoint.id})}>预览切为主入口</button><button className="button secondary" disabled={busy || endpoint.primary || data.reality.endpoints.length < 2} onClick={() => void prepare('reality.retire', {endpoint_id: endpoint.id})}>预览退役入口</button></div>
        </div>;
      })}</div>
      <Notice>切换主入口会更新关联订阅。退役会移除所选入口的路由和服务，仍使用该 SNI 的客户端将无法连接；请先刷新这些客户端的订阅。当前主入口与最后一个入口不能退役。</Notice>
      <form className="form-stack separated" onSubmit={event => {
        event.preventDefault();
        void prepare('reality.migrate', targetType === 'site' ? {type: 'site', site_id: siteId, sni: selectedSni} : {type: 'external', address: externalAddress, port: 443, sni: externalSni});
      }}>
        <h3>迁移到新目标</h3>
        <Field label="目标类型"><select value={targetType} disabled={busy} onChange={event => setTargetType(event.target.value as 'site' | 'external')}><option value="site">本机受管网站</option><option value="external">外部 TLS 网站</option></select></Field>
        {targetType === 'site' ? <>
          <Field label="受管网站" hint="仅列出已启用且绑定域名的网站；迁移后该网站和证书将成为入口依赖。"><select required value={siteId} disabled={busy} onChange={event => {setSiteId(event.target.value); setSiteSni('');}}><option value="">选择网站</option>{sites.map(item => <option value={item.id} key={item.id}>{item.title} · {item.id}</option>)}</select></Field>
          <Field label="新 SNI"><select required value={selectedSni} disabled={busy || !site} onChange={event => setSiteSni(event.target.value)}><option value="">选择网站绑定的域名</option>{site?.domains.map(domain => <option value={domain} key={domain}>{domain}</option>)}</select></Field>
        </> : <>
          <Field label="外部目标地址" hint="填写外部域名，不包含协议或端口；使用 HTTPS 443。"><input className="code-input" value={externalAddress} required maxLength={253} disabled={busy} onChange={event => setExternalAddress(event.target.value)} /></Field>
          <Field label="新 SNI" hint="与连接地址分别填写，必须由目标证书覆盖。"><input className="code-input" value={externalSni} required maxLength={253} disabled={busy} onChange={event => setExternalSni(event.target.value)} /></Field>
          <Notice>管理服务将验证 DNS、TLS 1.3、X25519 和证书，并拒绝回环或指向本机的外部目标。</Notice>
        </>}
        <Notice>应用成功后，新入口成为主入口并更新订阅，旧 SNI、旧入口服务及现有网站继续保留。请刷新订阅并验证真实代理请求，再决定何时退役旧入口。</Notice>
        <div className="form-actions"><button className="button primary" disabled={busy || (targetType === 'site' ? !site || !selectedSni : !externalAddress || !externalSni)}>验证并预览迁移</button></div>
      </form>
    </Section>
    <Hy2Section key={JSON.stringify(data.hy2.node)} data={data} prepare={prepare} busy={busy} />
  </>;
}

function Hy2Section({data, prepare, busy}: Pick<FormProps, 'data' | 'prepare' | 'busy'>) {
  const node = data.hy2.node;
  const [name, setName] = useState(node?.name ?? 'ProxyHub HY2');
  const [port, setPort] = useState(String(node?.port ?? 443));
  const [serverName, setServerName] = useState(node?.server_name ?? data.website.domain);
  const [certificateId, setCertificateId] = useState(node?.certificate_id ?? '');
  const binding = data.certificates.find(item => item.id === certificateId);
  const changed = !node || name !== node.name || Number(port) !== node.port || serverName !== node.server_name || certificateId !== node.certificate_id;
  const service = data.services.find(item => item.name === 'proxyhub-hy2');
  return <Section title="HY2 节点" description="使用独立 UDP 监听和证书，与 REALITY、网站共用现有订阅。">
    {node ? <>
      <Details rows={[
        ['节点编号', <code>{node.id}</code>], ['发布状态', node.enabled ? '已启用，加入订阅' : '已停用，不加入订阅'],
        ['服务状态', <Status value={service?.status ?? 'unknown'} />],
        ['连接地址', <code>{data.proxy.public_address}:{node.port} / UDP</code>],
        ['TLS 服务名', <code>{node.server_name}</code>], ['绑定证书', <code>{node.certificate_id}</code>],
        ['验证记录', '请使用客户端完成真实代理请求；服务运行不等于连接通过'],
      ]} />
      <div className="form-actions"><button className="button secondary" disabled={busy} onClick={() => void prepare(node.enabled ? 'hy2.disable' : 'hy2.enable', {})}>{node.enabled ? '预览停用 HY2' : '预览启用 HY2'}</button></div>
    </> : <Notice>当前尚未创建 HY2 节点。创建时自动生成独立认证密码，普通资源和变更预览不显示密码。</Notice>}
    <form className="form-stack separated" onSubmit={event => {
      event.preventDefault();
      void prepare(node ? 'hy2.update' : 'hy2.create', {name, port: Number(port), server_name: serverName, certificate_id: certificateId});
    }}>
      <h3>{node ? '更新 HY2 配置' : '创建 HY2 节点'}</h3>
      <Field label="HY2 节点名称"><input value={name} required maxLength={120} disabled={busy} onChange={event => setName(event.target.value)} /></Field>
      <Field label="UDP 监听端口" hint="默认 UDP 443；同端口的 TCP 网站与 REALITY 服务保持不变。"><input type="number" min={1} max={65535} step={1} value={port} required disabled={busy} onChange={event => setPort(event.target.value)} /></Field>
      <Field label="TLS 服务名" hint="填写绑定证书覆盖的域名。客户端连接地址沿用上方公网连接地址。"><input className="code-input" value={serverName} required maxLength={253} disabled={busy} onChange={event => setServerName(event.target.value)} /></Field>
      <Field label="HY2 绑定证书" hint="引用受管证书；证书替换和续期将同时更新引用它的服务。"><select required value={certificateId} disabled={busy} onChange={event => setCertificateId(event.target.value)}><option value="">选择证书</option>{data.certificates.map(item => <option key={item.id} value={item.id} disabled={item.status !== 'valid'}>{item.name} · {item.id}{item.status !== 'valid' ? ' · 校验失败' : ''}</option>)}</select></Field>
      {binding && <p className="muted">证书覆盖：{binding.domains.join('、')}。HY2 停用后仍保留证书绑定。</p>}
      <Notice>应用只调整独立 HY2 服务和订阅，REALITY 核心不重启。UDP 端口需能从客户端访问；更新或停用会中断 HY2 连接。应用后刷新订阅并完成真实代理请求。</Notice>
      <div className="form-actions"><button className="button primary" disabled={busy || !changed || !certificateId}>{node ? '验证并预览 HY2 更新' : '验证并预览创建 HY2'}</button></div>
    </form>
  </Section>;
}
