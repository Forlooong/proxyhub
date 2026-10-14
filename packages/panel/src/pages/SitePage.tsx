import { useState } from 'react';
import type { FormProps } from '../types';
import { date, Details, Field, Notice, Section } from '../components/common';
import { Icon } from '../components/Icon';

export function SitePage({data, prepare, busy}: FormProps) {
  const [title, setTitle] = useState(data.website.title);
  const [certificate, setCertificate] = useState('');
  const [privateKey, setPrivateKey] = useState('');
  return <>
    <Section title="受管网站" description="此网站同时作为 REALITY 的本机握手目标。" action={<a className="text-button" href={data.website.url} target="_blank" rel="noreferrer">访问网站<Icon name="external" /></a>}>
      <Details rows={[
        ['绑定域名', <code>{data.website.domain}</code>], ['关联节点', data.proxy.name], ['HTTPS 路径', '公网 SNI 路由 → REALITY → 本机 HTTPS 网站'],
      ]} />
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); void prepare('website.update', {site_title: title});}}>
        <Field label="网站标题" hint="更新默认静态页面标题，域名、证书与节点关系保持不变。"><input required value={title} maxLength={120} disabled={busy} onChange={event => setTitle(event.target.value)} /></Field>
        <div className="form-actions"><button className="button primary" disabled={busy || title === data.website.title}>预览网站变更</button></div>
      </form>
    </Section>
    <Section title="TLS 证书" description="证书绑定到网站与订阅 HTTPS 服务。更新通过校验后才会激活。">
      {data.certificate.status === 'invalid' && <Notice tone="error">{data.certificate.error ?? '当前证书校验失败，请检查有效期与绑定域名，再续期或导入有效证书。'}</Notice>}
      <Details rows={[
        ['域名', <div className="stacked-values">{data.certificate.domains.map(domain => <code key={domain}>{domain}</code>)}</div>],
        ['有效期至', date(data.certificate.not_after)], ['续期方式', data.certificate.automatic_renewal ? 'HTTP-01 自动续期' : '外部管理，需手工导入续期证书'],
        ['使用者', data.certificate.consumers.join('、')], ['SHA-256 指纹', <code className="fingerprint">{data.certificate.sha256 ?? '无法读取'}</code>],
      ]} />
      {data.certificate.automatic_renewal ? <div className="form-actions"><button className="button secondary" disabled={busy} onClick={() => void prepare('certificate.renew', {})}><Icon name="refresh" />预览证书续期</button><span className="muted">未到续期时间时会保留当前证书。</span></div> : <Notice>此证书由外部签发流程维护。请在原签发方完成续期，再导入有效的证书与私钥。</Notice>}
      <details className="advanced separated"><summary>导入证书与私钥</summary>
        <Notice tone="warning">新导入证书默认由外部管理；已有受管证书保留已验证的续期归属，以预览为准。证书必须覆盖当前网站与订阅域名，并与私钥匹配。</Notice>
        <form className="form-stack" onSubmit={event => {event.preventDefault(); const values = {certificate_pem: certificate, private_key_pem: privateKey}; setPrivateKey(''); void prepare('certificate.import', values);}}>
          <Field label="完整证书链 PEM"><textarea className="code-input" value={certificate} onChange={event => setCertificate(event.target.value)} required rows={5} maxLength={65536} disabled={busy} spellCheck={false} autoComplete="off" placeholder="-----BEGIN CERTIFICATE-----" /></Field>
          <Field label="证书私钥 PEM" hint="仅发送至私有管理服务；提交后立即清空此输入，不保存在浏览器。"><textarea className="code-input" value={privateKey} onChange={event => setPrivateKey(event.target.value)} required rows={4} maxLength={32768} disabled={busy} spellCheck={false} autoComplete="off" placeholder="-----BEGIN PRIVATE KEY-----" /></Field>
          <div className="form-actions"><button className="button primary" type="submit" disabled={busy}>验证并预览导入</button></div>
        </form>
      </details>
    </Section>
  </>;
}
