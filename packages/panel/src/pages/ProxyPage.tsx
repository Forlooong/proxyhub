import { useState } from 'react';
import type { FormProps, JsonObject, ProtocolNode } from '../types';
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
    <NodesSection data={data} prepare={prepare} busy={busy} />
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

const protocols: {id: ProtocolNode['protocol']; label: string}[] = [
  {id: 'vless', label: 'VLESS'}, {id: 'ss', label: 'Shadowsocks'}, {id: 'trojan', label: 'Trojan'},
  {id: 'tuic', label: 'TUIC v5'}, {id: 'anytls', label: 'AnyTLS'}, {id: 'vmess', label: 'VMess AEAD'}, {id: 'socks5', label: 'SOCKS5'},
];
const ssMethods = ['aes-128-gcm', 'aes-256-gcm', 'chacha20-ietf-poly1305', '2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm', '2022-blake3-chacha20-poly1305'];

function NodesSection({data, prepare, busy}: Pick<FormProps, 'data' | 'prepare' | 'busy'>) {
  const [selectedId, setSelectedId] = useState('');
  const selected = data.nodes.nodes.find(node => node.id === selectedId);
  return <Section title="独立协议节点" description="按能力矩阵创建独立监听，保留现有 REALITY、HY2、网站和订阅服务。">
    {data.nodes.nodes.length ? <div className="job-list">{data.nodes.nodes.map(node => {
      const encrypted = node.protocol !== 'socks5' && !(node.protocol === 'vless' && !node.options.tls) && !(node.protocol === 'vmess' && !node.options.tls && node.options.security === 'none');
      const service = data.services.find(item => item.name === `proxyhub-node@${node.id}`);
      return <div className="job-item" key={node.id}>
        <div className="form-actions"><strong>{node.name}</strong><span className={`status ${node.enabled ? 'status-good' : 'status-neutral'}`}>{node.enabled ? '已发布' : '已停用'}</span></div>
        <Details rows={[
          ['节点编号', <code>{node.id}</code>], ['协议', protocols.find(item => item.id === node.protocol)?.label ?? node.protocol],
          ['公网监听', <code>{data.proxy.public_address}:{node.port}{node.protocol === 'tuic' ? ' / UDP' : node.protocol === 'ss' ? ' / TCP + UDP' : ' / TCP'}{node.protocol === 'socks5' && node.options.udp ? ' · UDP 转发已启用' : ''}</code>],
          ['传输安全', encrypted ? node.protocol === 'vmess' && !node.options.tls ? 'VMess 协议加密，未启用 TLS' : '加密传输' : '未加密传输'], ['服务状态', <Status value={service?.status ?? 'unknown'} />],
          ['认证状态', node.protocol === 'socks5' && !node.options.auth ? '未启用认证，所有可达客户端均可使用' : '已配置独立认证凭据'],
          ['协议选项', <code>{JSON.stringify(node.options)}</code>],
        ]} />
        {node.options.transport === 'h2' && <Notice>锁定 Mihomo 的 H2 UDP 未通过验收；YAML 不声明 UDP 支持，仅承诺 H2 TCP。</Notice>}
        <div className="form-actions">
          <button className="button secondary" disabled={busy} onClick={() => setSelectedId(node.id)}>编辑节点</button>
          <button className="button secondary" disabled={busy} onClick={() => void prepare(node.enabled ? 'nodes.disable' : 'nodes.enable', {node_id: node.id})}>{node.enabled ? '预览停用节点' : '预览启用节点'}</button>
          <button className="button secondary" disabled={busy || node.enabled} onClick={() => void prepare('nodes.delete', {node_id: node.id})}>预览删除节点</button>
        </div>
      </div>;
    })}</div> : <Notice>尚无独立协议节点。创建时自动生成认证凭据，普通资源和预览不显示凭据。已有 REALITY 和 HY2 在上方管理。</Notice>}
    {selected && <div className="form-actions separated"><button className="button secondary" disabled={busy} onClick={() => setSelectedId('')}>改为创建新节点</button></div>}
    <NodeForm key={selected?.id ?? 'create'} data={data} prepare={prepare} busy={busy} node={selected} />
  </Section>;
}

function NodeForm({data, prepare, busy, node}: Pick<FormProps, 'data' | 'prepare' | 'busy'> & {node?: ProtocolNode}) {
  const options = node?.options ?? {};
  const [protocol, setProtocol] = useState<ProtocolNode['protocol']>(node?.protocol ?? 'vless');
  const [name, setName] = useState(node?.name ?? '');
  const [port, setPort] = useState(String(node?.port ?? 5443));
  const [transport, setTransport] = useState(String(options.transport ?? 'tcp'));
  const [tls, setTls] = useState(options.tls !== false);
  const [tlsMode, setTlsMode] = useState(String(options.tls_mode ?? 'certificate'));
  const [sni, setSni] = useState(String(options.sni ?? data.website.domain));
  const [certificateId, setCertificateId] = useState(String(options.certificate_id ?? ''));
  const [targetId, setTargetId] = useState(String(options.reality_target_id ?? ''));
  const [host, setHost] = useState(String(options.host ?? data.website.domain));
  const [path, setPath] = useState(String(options.path ?? '/'));
  const [flow, setFlow] = useState(String(options.flow ?? ''));
  const [security, setSecurity] = useState(String(options.security ?? 'auto'));
  const [method, setMethod] = useState(String(options.method ?? 'aes-256-gcm'));
  const [udp, setUdp] = useState(options.udp !== false);
  const auth = options.auth !== false;
  const [congestion, setCongestion] = useState(String(options.congestion_control ?? 'bbr'));
  const [relay, setRelay] = useState(String(options.udp_relay_mode ?? 'native'));
  const transported = ['vless', 'vmess', 'trojan'].includes(protocol);
  const usesTls = ['trojan', 'tuic', 'anytls'].includes(protocol) || (transported && tls);
  const realityMode = protocol === 'vless' && tlsMode === 'reality' && usesTls;
  const targets = data.reality.endpoints.filter((endpoint, index, all) => all.findIndex(item => item.target_id === endpoint.target_id) === index);
  const target = targets.find(item => item.target_id === targetId);
  const selectedSni = realityMode ? target?.sni ?? '' : sni;
  const transports = protocol === 'trojan' ? ['tcp', 'ws', 'httpupgrade'] : protocol === 'vmess' ? ['tcp', 'http', 'ws', 'h2', 'httpupgrade'] : ['tcp', 'ws', 'h2', 'httpupgrade'];
  const binding = data.certificates.find(item => item.id === certificateId);
  function protocolChanged(value: ProtocolNode['protocol']) {
    setProtocol(value); setTransport('tcp'); setTls(true); setTlsMode('certificate'); setFlow(''); setSecurity('auto');
  }
  function transportChanged(value: string) {
    setTransport(value); setFlow('');
    if (value === 'h2') setTls(true);
    if (value === 'http') {setTls(false); setSecurity('none');}
    if (!['tcp', 'h2'].includes(value)) setTlsMode('certificate');
  }
  function submit() {
    const values: JsonObject = {name, port: Number(port)};
    const settings: JsonObject = {};
    if (transported) {
      settings.transport = transport;
      settings.tls = usesTls;
      if (transport !== 'tcp') {settings.host = host; settings.path = path;}
      if (protocol === 'vless') settings.flow = transport === 'tcp' && usesTls ? flow : '';
      if (protocol === 'vmess') settings.security = security;
    }
    if (usesTls && protocol !== 'ss' && protocol !== 'socks5') {
      settings.tls = true; settings.tls_mode = realityMode ? 'reality' : 'certificate'; settings.sni = selectedSni;
      if (realityMode) settings.reality_target_id = targetId;
      else settings.certificate_id = certificateId;
    }
    if (protocol === 'ss') settings.method = method;
    if (protocol === 'socks5') {settings.auth = auth; settings.udp = udp;}
    if (protocol === 'tuic') {settings.congestion_control = congestion; settings.udp_relay_mode = relay;}
    values.options = settings;
    if (node) values.node_id = node.id;
    else values.protocol = protocol;
    void prepare(node ? 'nodes.update' : 'nodes.create', values);
  }
  return <form className="form-stack separated" onSubmit={event => {event.preventDefault(); submit();}}>
    <h3>{node ? `编辑 ${node.name}` : '创建独立节点'}</h3>
    <Field label="协议"><select value={protocol} disabled={busy || !!node} onChange={event => protocolChanged(event.target.value as ProtocolNode['protocol'])}>{protocols.map(item => <option value={item.id} key={item.id}>{item.label}</option>)}</select></Field>
    <Field label="独立节点名称"><input required value={name} maxLength={120} disabled={busy} onChange={event => setName(event.target.value)} /></Field>
    <Field label="独立公网端口" hint="请选择未占用的独立端口；不复用网站、REALITY、HY2 或其他节点的监听。"><input type="number" min={1} max={65535} step={1} required value={port} disabled={busy} onChange={event => setPort(event.target.value)} /></Field>
    {transported && <>
      <Field label="传输" hint="按固定核心能力提供；新建 gRPC 和 Trojan H2 不受支持。"><select value={transport} disabled={busy} onChange={event => transportChanged(event.target.value)}>{transports.map(item => <option value={item} key={item}>{item === 'http' ? 'HTTP/1 头' : item.toUpperCase()}</option>)}</select></Field>
      {transport === 'h2' && <Notice>锁定 Mihomo 的 H2 UDP 未通过验收；YAML 不声明 UDP 支持，仅承诺 H2 TCP。</Notice>}
      {protocol !== 'trojan' && <Field label="TLS"><select value={usesTls ? 'yes' : 'no'} disabled={busy || transport === 'h2' || transport === 'http'} onChange={event => {setTls(event.target.value === 'yes'); setFlow('');}}><option value="yes">启用 TLS</option><option value="no">关闭 TLS</option></select></Field>}
      {transport !== 'tcp' && <><Field label="传输 Host"><input className="code-input" required value={host} maxLength={253} disabled={busy} onChange={event => setHost(event.target.value)} /></Field><Field label="传输路径"><input className="code-input" required value={path} maxLength={256} disabled={busy} onChange={event => setPath(event.target.value)} /></Field></>}
    </>}
    {usesTls && protocol !== 'ss' && protocol !== 'socks5' && <>
      {protocol === 'vless' && <Field label="TLS 模式"><select value={tlsMode} disabled={busy} onChange={event => {setTlsMode(event.target.value); setFlow('');}}><option value="certificate">受管证书</option>{['tcp', 'h2'].includes(transport) && <option value="reality">现有 REALITY 目标</option>}</select></Field>}
      {realityMode ? <Field label="REALITY 握手目标" hint="复用已有目标，新节点使用独立公网端口；目标和站点依赖会继续受保护。"><select required value={targetId} disabled={busy} onChange={event => setTargetId(event.target.value)}><option value="">选择目标</option>{targets.map(item => <option key={item.target_id} value={item.target_id}>{item.sni} · {item.address}:{item.port}</option>)}</select></Field> : <>
        <Field label="节点 TLS 服务名"><input className="code-input" required value={sni} maxLength={253} disabled={busy} onChange={event => setSni(event.target.value)} /></Field>
        <Field label="节点绑定证书" hint="证书缺失或无效时，请先在网站页面添加、导入或申请证书。"><select required value={certificateId} disabled={busy} onChange={event => setCertificateId(event.target.value)}><option value="">选择有效证书</option>{data.certificates.map(item => <option key={item.id} value={item.id} disabled={item.status !== 'valid'}>{item.name} · {item.id}{item.status !== 'valid' ? ' · 校验失败' : ''}</option>)}</select></Field>
        {binding && <p className="muted">证书覆盖：{binding.domains.join('、')}。停用节点仍保留绑定，删除节点才解除依赖。</p>}
      </>}
      {protocol === 'vless' && transport === 'tcp' && <Field label="VLESS Flow"><select value={flow} disabled={busy} onChange={event => setFlow(event.target.value)}><option value="">无</option><option value="xtls-rprx-vision">xtls-rprx-vision</option></select></Field>}
    </>}
    {protocol === 'vmess' && <Field label="VMess 加密" hint="固定 alterId=0；HTTP/1 头只支持 none。"><select value={security} disabled={busy || transport === 'http'} onChange={event => setSecurity(event.target.value)}>{['auto', 'none', 'aes-128-gcm', 'chacha20-poly1305'].map(item => <option key={item} value={item}>{item}</option>)}</select></Field>}
    {protocol === 'ss' && <Field label="Shadowsocks 方法" hint="不支持插件；创建时自动生成对应密钥。更新保留密钥，长度不兼容的方法切换会被拒绝。"><select value={method} disabled={busy} onChange={event => setMethod(event.target.value)}>{ssMethods.map(item => <option key={item} value={item}>{item}</option>)}</select></Field>}
    {protocol === 'socks5' && <><Notice>SOCKS5 本身不加密。{auth ? '此节点使用自动生成的用户名和密码认证。' : '此节点未启用认证，所有能访问端口的客户端均可使用；此次编辑保留原认证状态。'}</Notice><Field label="SOCKS5 UDP" hint="通过 UDP association 转发，实际 UDP 端口由服务协商。"><select value={udp ? 'yes' : 'no'} disabled={busy} onChange={event => setUdp(event.target.value === 'yes')}><option value="yes">启用 UDP 转发</option><option value="no">仅 TCP</option></select></Field></>}
    {protocol === 'tuic' && <><Field label="TUIC 拥塞控制"><select value={congestion} disabled={busy} onChange={event => setCongestion(event.target.value)}>{['bbr', 'cubic', 'new_reno'].map(item => <option key={item} value={item}>{item}</option>)}</select></Field><Field label="TUIC UDP 转发"><select value={relay} disabled={busy} onChange={event => setRelay(event.target.value)}><option value="native">native</option><option value="quic">quic</option></select></Field></>}
    {!usesTls && (protocol === 'vless' || (protocol === 'vmess' && security === 'none')) && <Notice>此节点的传输未加密。请仅在可信网络中使用。</Notice>}
    {protocol === 'vmess' && !usesTls && security !== 'none' && <Notice>此节点使用 VMess 协议加密，未启用 TLS。</Notice>}
    <Notice>创建时生成稳定认证凭据，更新保留凭据。启用节点加入现有订阅；更新、证书替换或停用会中断该独立节点的连接，已有 REALITY、HY2 和网站继续运行。</Notice>
    <div className="form-actions"><button className="button primary" disabled={busy || (!node && data.nodes.nodes.length >= 32) || (usesTls && !['ss', 'socks5'].includes(protocol) && (!selectedSni || (realityMode ? !targetId : !certificateId)))}>{node ? '验证并预览节点更新' : '验证并预览创建节点'}</button></div>
  </form>;
}
