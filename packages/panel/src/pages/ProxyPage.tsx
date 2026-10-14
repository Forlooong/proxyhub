import { useState } from 'react';
import type { FormProps } from '../types';
import { Details, Field, Notice, Section, Status } from '../components/common';

export function ProxyPage({data, prepare, busy}: FormProps) {
  const [name, setName] = useState(data.proxy.name);
  const [address, setAddress] = useState(data.proxy.public_address);
  return <>
    <Section title="REALITY 节点" description="管理当前单节点的客户端显示名称与连接地址。">
      <Details rows={[
        ['协议', data.proxy.protocol], ['公网端口', data.proxy.port], ['SNI', <code>{data.proxy.sni}</code>], ['本机握手目标', <code>{data.proxy.target}</code>], ['验证记录', <Status value={data.proxy.verification} />],
      ]} />
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); void prepare('proxy.update', {node_name: name, public_address: address});}}>
        <Field label="节点名称"><input value={name} required maxLength={120} disabled={busy} onChange={event => setName(event.target.value)} /></Field>
        <Field label="客户端连接地址" hint="填写公网 IP 或可解析到本机的域名，不包含协议或端口。"><input className="code-input" value={address} required maxLength={253} disabled={busy} onChange={event => setAddress(event.target.value)} /></Field>
        <Notice>节点变更会更新关联订阅。应用后请让客户端刷新订阅，并完成一次真实代理请求。</Notice>
        <div className="form-actions"><button className="button primary" disabled={busy || (name === data.proxy.name && address === data.proxy.public_address)}>预览节点变更</button></div>
      </form>
    </Section>
    <Section title="握手目标关系"><Details rows={[
      ['当前方式', '本机受管网站'], ['关联域名', data.website.domain], ['关联证书', dateShort(data.certificate.not_after)],
    ]} /><p className="muted">普通浏览器与 REALITY 客户端先进入同一监听，由 REALITY 处理认证与网站回落。本机目标使用内部地址，避免公网端口循环。</p></Section>
  </>;
}
function dateShort(value: string | null) {return value ? `有效期至 ${new Date(value).toLocaleDateString('zh-CN')}` : '证书校验未通过，请到网站与证书页处理';}
