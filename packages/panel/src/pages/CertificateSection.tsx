import { useState } from 'react';
import type { Certificate, Prepare } from '../types';
import { date, Details, Field, Notice, Section } from '../components/common';

export function CertificateSection({certificates, prepare, busy}: {certificates: Certificate[]; prepare: Prepare; busy: boolean}) {
  const [mode, setMode] = useState('add');
  const [name, setName] = useState('');
  const [domains, setDomains] = useState('');
  const [certificateId, setCertificateId] = useState(certificates[0]?.id ?? '');
  const [certificate, setCertificate] = useState('');
  const [privateKey, setPrivateKey] = useState('');
  return <Section title="TLS 证书" description="按稳定编号管理证书与依赖。证书校验后生成变更预览，确认应用后才会激活。">
    <div className="job-list">{certificates.map(item => <div className="job-item" key={item.id}>
      <div className="form-actions"><strong>{item.name}</strong><span className={`status ${item.status === 'valid' ? 'status-good' : 'status-bad'}`}>{item.status === 'valid' ? '校验通过' : '校验失败'}</span></div>
      {item.status === 'invalid' && <Notice tone="error">{item.error ?? '请替换为有效且匹配全部消费者域名的证书。'}</Notice>}
      <Details rows={[
        ['证书编号', <code>{item.id}</code>], ['域名', <div className="stacked-values">{item.domains.map(domain => <code key={domain}>{domain}</code>)}</div>],
        ['有效期至', date(item.not_after)], ['续期方式', item.automatic_renewal ? 'HTTP-01 自动续期' : '手工管理'],
        ['使用者', item.consumers.length ? <div className="stacked-values">{item.consumers.map(consumer => <span key={`${consumer.kind}-${consumer.id}-${consumer.domain}`}>{consumer.label} · <code>{consumer.kind}/{consumer.id}</code> · {consumer.domain}:{consumer.port}{consumer.enabled === false ? ' · 已停用，仍保留绑定' : ''}</span>)}</div> : '无绑定消费者'],
        ['SHA-256 指纹', <code className="fingerprint">{item.sha256 ?? '无法读取'}</code>],
      ]} />
      <div className="form-actions"><button className="button secondary" disabled={busy || !item.automatic_renewal} onClick={() => void prepare('certificate.renew', {certificate_id: item.id})}>预览续期</button><button className="button secondary" disabled={busy} onClick={() => {setMode('replace'); setCertificateId(item.id); setCertificate(''); setPrivateKey('');}}>替换此证书</button><button className="button secondary" disabled={busy || item.consumers.length > 0} onClick={() => void prepare('certificate.delete', {certificate_id: item.id})}>预览删除证书</button></div>
      {!item.automatic_renewal && <p className="muted">手工管理的证书需在原签发方续期，再替换证书与私钥。</p>}
      {item.consumers.length > 0 && <p className="muted">此证书仍被使用；请先为以上消费者更换绑定证书，再删除。</p>}
    </div>)}</div>
    <form className="form-stack separated" onSubmit={event => {
      event.preventDefault();
      const names = domains.split(/[\s,，]+/).filter(Boolean);
      const pem = {certificate_pem: certificate, private_key_pem: privateKey};
      setPrivateKey('');
      if (mode === 'request') void prepare('certificate.issue', {name, domains: names});
      else if (mode === 'replace') void prepare('certificate.import', {certificate_id: certificateId, ...pem});
      else void prepare('certificate.add', {name, domains: names, ...pem});
    }}>
      <Field label="证书操作"><select value={mode} disabled={busy} onChange={event => {setMode(event.target.value); setPrivateKey('');}}><option value="add">新增手工证书</option><option value="request">申请 HTTP-01 证书</option><option value="replace" disabled={!certificates.length}>替换指定证书</option></select></Field>
      {mode === 'replace' ? <><Field label="替换证书"><select required value={certificateId} disabled={busy} onChange={event => {setCertificateId(event.target.value); setPrivateKey('');}}>{certificates.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></Field><Notice>替换保留证书编号及消费者绑定；新证书必须覆盖全部依赖域名，并与私钥匹配。续期归属以变更预览为准。</Notice></> : <><Field label="证书名称"><input required value={name} maxLength={120} disabled={busy} onChange={event => setName(event.target.value)} /></Field><Field label="证书域名" hint="填写一个或多个域名，以空格或逗号分隔。"><input required value={domains} maxLength={4096} disabled={busy} onChange={event => setDomains(event.target.value)} /></Field></>}
      {mode === 'request' ? <Notice>所有域名需解析到本机。HTTP-01 使用现有 HTTP 入口完成验证，申请成功后证书可供网站绑定。</Notice> : <><Field label="完整证书链 PEM"><textarea className="code-input" value={certificate} onChange={event => setCertificate(event.target.value)} required rows={5} maxLength={65536} disabled={busy} spellCheck={false} autoComplete="off" placeholder="-----BEGIN CERTIFICATE-----" /></Field><Field label="证书私钥 PEM" hint="仅发送至私有管理服务；提交后立即清空此输入，不保存在浏览器。"><textarea className="code-input" value={privateKey} onChange={event => setPrivateKey(event.target.value)} required rows={4} maxLength={32768} disabled={busy} spellCheck={false} autoComplete="off" placeholder="-----BEGIN PRIVATE KEY-----" /></Field></>}
      <div className="form-actions"><button className="button primary" disabled={busy || (mode === 'replace' && !certificateId)}>{mode === 'request' ? '预览证书申请' : mode === 'replace' ? '验证并预览替换' : '验证并预览新增证书'}</button></div>
    </form>
  </Section>;
}
