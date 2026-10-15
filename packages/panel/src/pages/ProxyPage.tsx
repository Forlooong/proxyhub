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
  </>;
}
