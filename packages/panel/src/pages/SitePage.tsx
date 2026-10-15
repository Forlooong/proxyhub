import { useEffect, useRef, useState } from 'react';
import { message, operation } from '../api';
import type { FormProps } from '../types';
import { date, Details, Field, formatBytes, Notice, Section } from '../components/common';
import { Icon } from '../components/Icon';

type ContentVersion = {id: string; created: number | null; file_count: number; total_bytes: number; current: boolean; source: 'upload' | 'template'};

export function SitePage({data, prepare, busy}: FormProps) {
  const [title, setTitle] = useState(data.website.title);
  const [certificate, setCertificate] = useState('');
  const [privateKey, setPrivateKey] = useState('');
  const [archive, setArchive] = useState<File | null>(null);
  const [reading, setReading] = useState(false);
  const [contentError, setContentError] = useState('');
  const [versions, setVersions] = useState<ContentVersion[]>([]);
  const [versionsError, setVersionsError] = useState('');
  const [loadingVersions, setLoadingVersions] = useState(true);
  const [versionsRequest, setVersionsRequest] = useState(0);
  const readEpoch = useRef(0);
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const defaultTemplate = data.website.content_version === null;
  useEffect(() => () => {readEpoch.current++;}, []);
  useEffect(() => {
    let active = true;
    setLoadingVersions(true); setVersionsError('');
    void operation<{current_version: string | null; versions: ContentVersion[]}>('website.content.versions').then(result => {
      if (active) setVersions(result.versions);
    }).catch(error => {if (active) setVersionsError(message(error));}).finally(() => {if (active) setLoadingVersions(false);});
    return () => {active = false;};
  }, [data.revision, versionsRequest]);
  async function upload() {
    if (!archive || busy || reading) return;
    const epoch = ++readEpoch.current;
    setContentError('');
    if (!/\.zip$/i.test(archive.name) || archive.size === 0 || archive.size > 180 * 1024) {
      setContentError('请选择不超过 180 KiB 的非空 ZIP 文件。'); return;
    }
    setReading(true);
    try {
      const bytes = new Uint8Array(await archive.arrayBuffer());
      if (epoch !== readEpoch.current || busyRef.current) return;
      if (bytes.length === 0 || bytes.length > 180 * 1024) throw new Error('ZIP 文件大小必须在 1 B 至 180 KiB 之间。');
      let binary = '';
      for (const byte of bytes) binary += String.fromCharCode(byte);
      await prepare('website.content.upload', {archive_base64: btoa(binary)});
    } catch (error) {if (epoch === readEpoch.current) setContentError(message(error));}
    finally {if (epoch === readEpoch.current) setReading(false);}
  }
  return <>
    <Section title="受管网站" description="此网站同时作为 REALITY 的本机握手目标。" action={<a className="text-button" href={data.website.url} target="_blank" rel="noreferrer">访问网站<Icon name="external" /></a>}>
      <Details rows={[
        ['绑定域名', <code>{data.website.domain}</code>], ['关联节点', data.proxy.name], ['HTTPS 路径', '公网 SNI 路由 → REALITY → 本机 HTTPS 网站'],
        ['当前内容', defaultTemplate ? '默认模板' : <code>{data.website.content_version}</code>],
      ]} />
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); if (defaultTemplate) void prepare('website.update', {site_title: title});}}>
        <Field label="网站标题" hint="仅默认模板可更新标题；上传内容需修改后重新上传。"><input required value={title} maxLength={120} disabled={busy || !defaultTemplate} onChange={event => setTitle(event.target.value)} /></Field>
        <div className="form-actions"><button className="button primary" disabled={busy || !defaultTemplate || title === data.website.title}>预览网站变更</button></div>
      </form>
    </Section>
    <Section title="网站内容" description="上传静态网站或恢复历史版本，先检查变更与页面预览，再确认应用。">
      <form className="form-stack" onSubmit={event => {event.preventDefault(); void upload();}}>
        <Field label="静态网站 ZIP" hint="压缩包不超过 180 KiB，解压后不超过 2 MiB、200 项，根目录必须包含 index.html。">
          <input type="file" accept=".zip,application/zip" disabled={busy} onChange={event => {readEpoch.current++; setArchive(event.target.files?.[0] ?? null); setContentError(''); setReading(false);}} />
        </Field>
        {contentError && <Notice tone="error">{contentError}</Notice>}
        <div className="form-actions"><button className="button primary" disabled={busy || reading || !archive}>{reading ? '正在读取压缩包…' : '验证并预览上传'}</button></div>
      </form>
      <div className="section-head separated"><h3>内容版本</h3><button className="text-button" disabled={loadingVersions || busy} onClick={() => setVersionsRequest(value => value + 1)}><Icon name="refresh" />刷新版本</button></div>
      {versionsError && <Notice tone="error">{versionsError}</Notice>}
      {loadingVersions && versions.length === 0 && <p className="empty" role="status">正在读取内容版本…</p>}
      <div className="job-list">{versions.map(version => <div className="job-item" key={version.id}>
        <div className="form-actions"><strong>{version.source === 'template' ? '默认模板' : <code>{version.id}</code>}</strong>{version.current && <span className="status status-good">当前使用</span>}</div>
        <Details rows={[
          ['创建时间', version.source === 'template' ? '内置模板' : date(version.created)], ['文件数', version.file_count], ['内容体积', formatBytes(version.total_bytes)],
        ]} />
        <div className="form-actions"><button className="button secondary" disabled={busy || reading || version.current} onClick={() => void prepare('website.content.restore', {version_id: version.id})}>预览恢复此版本</button></div>
      </div>)}</div>
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
