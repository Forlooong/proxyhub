import { useEffect, useRef, useState } from 'react';
import { message, operation } from '../api';
import type { FormProps } from '../types';
import { date, Details, Field, formatBytes, Notice, Section } from '../components/common';
import { Icon } from '../components/Icon';
import { CertificateSection } from './CertificateSection';

type ContentVersion = {id: string; created: number | null; file_count: number; total_bytes: number; current: boolean; source: 'upload' | 'template'};

export function SitePage({data, prepare, busy, selectedSiteId, selectSite}: FormProps & {selectedSiteId: string; selectSite: (id: string) => void}) {
  const site = data.websites.find(item => item.id === selectedSiteId) ?? data.websites.find(item => item.id === 'default')!;
  const [title, setTitle] = useState(site.title);
  const [domain, setDomain] = useState('');
  const [certificateId, setCertificateId] = useState(site.certificate_id ?? '');
  const [newTitle, setNewTitle] = useState('');
  const [newDomain, setNewDomain] = useState('');
  const [newCertificateId, setNewCertificateId] = useState(data.certificates.find(item => item.status === 'valid')?.id ?? '');
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
  const defaultTemplate = site.content_version === null;
  const realityEndpoints = data.reality.endpoints.filter(item => item.target_type === 'site' && item.site_id === site.id);
  const realitySnis = new Set(realityEndpoints.map(item => item.sni));
  const protectedSite = site.id === 'default' || site.dependencies.length > 0 || realityEndpoints.length > 0;
  useEffect(() => () => {readEpoch.current++;}, []);
  useEffect(() => {
    let active = true;
    setLoadingVersions(true); setVersionsError('');
    void operation<{current_version: string | null; versions: ContentVersion[]}>('website.content.versions', {site_id: site.id}).then(result => {
      if (active) setVersions(result.versions);
    }).catch(error => {if (active) setVersionsError(message(error));}).finally(() => {if (active) setLoadingVersions(false);});
    return () => {active = false;};
  }, [data.revision, site.id, versionsRequest]);
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
      await prepare('website.content.upload', {site_id: site.id, archive_base64: btoa(binary)});
    } catch (error) {if (epoch === readEpoch.current) setContentError(message(error));}
    finally {if (epoch === readEpoch.current) setReading(false);}
  }
  return <>
    <Section title="受管网站" description="选择网站以管理域名、证书和内容；选择操作仅切换管理上下文。" action={site.url && <a className="text-button" href={site.url} target="_blank" rel="noreferrer">访问网站<Icon name="external" /></a>}>
      <Field label="当前管理的网站"><select value={site.id} disabled={busy || reading} onChange={event => selectSite(event.target.value)}>{data.websites.map(item => <option key={item.id} value={item.id}>{item.title} · {item.id}{item.enabled ? '' : ' · 已停用'}</option>)}</select></Field>
      <Details rows={[
        ['站点编号', <code>{site.id}</code>], ['状态', site.enabled ? '已启用' : '已停用'],
        ['HTTPS 监听', <code>{site.listener_id} · 127.0.0.1:{site.tls_port}</code>],
        ['当前证书', <code>{site.certificate_id ?? '未绑定'}</code>],
        ['当前内容', defaultTemplate ? '默认模板' : <code>{site.content_version}</code>],
        ['依赖关系', site.dependencies.length ? site.dependencies.join('、') : '无其他资源依赖此站点'],
      ]} />
      {realityEndpoints.length > 0 && <Notice>此网站仍被 REALITY 入口 {realityEndpoints.map(item => item.id).join('、')} 引用。迁移并显式退役相关入口前，须保留网站、目标 SNI 与证书覆盖。</Notice>}
      {site.id === 'default' && <Notice>默认网站用于兼容原配置，须保持启用并保留首域名；实际 REALITY 引用以依赖关系列表为准。</Notice>}
      <div className="form-actions separated"><button className="button secondary" disabled={busy || (site.enabled && protectedSite)} onClick={() => void prepare(site.enabled ? 'website.disable' : 'website.enable', {site_id: site.id})}>{site.enabled ? '预览停用网站' : '预览启用网站'}</button><button className="button secondary" disabled={busy || protectedSite} onClick={() => void prepare('website.delete', {site_id: site.id})}>预览删除网站</button></div>
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); if (defaultTemplate) void prepare('website.update', {site_id: site.id, site_title: title});}}>
        <Field label="网站标题" hint="仅默认模板可更新标题；上传内容需修改后重新上传。"><input required value={title} maxLength={120} disabled={busy || !defaultTemplate} onChange={event => setTitle(event.target.value)} /></Field>
        <div className="form-actions"><button className="button primary" disabled={busy || !defaultTemplate || title === site.title}>预览网站变更</button></div>
      </form>
      <div className="separated"><h3>绑定域名</h3><div className="job-list">{site.domains.map(item => <div className="job-item" key={item}><div className="form-actions"><code>{item}</code><button className="text-button" disabled={busy || realitySnis.has(item) || (site.id === 'default' && item === site.domains[0])} onClick={() => void prepare('website.domain.unbind', {site_id: site.id, domain: item})}>预览解绑</button>{realitySnis.has(item) ? <span className="muted">仍被 REALITY 入口引用</span> : site.id === 'default' && item === site.domains[0] && <span className="muted">原配置兼容域名，须保留</span>}</div></div>)}</div></div>
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); void prepare('website.domain.bind', {site_id: site.id, domain});}}><Field label="新增域名" hint="域名需解析到本机，且由当前证书覆盖；管理服务将检查路由冲突。"><input required value={domain} maxLength={253} disabled={busy} onChange={event => setDomain(event.target.value)} /></Field><div className="form-actions"><button className="button secondary" disabled={busy || !domain}>预览绑定域名</button></div></form>
      <form className="form-stack separated" onSubmit={event => {event.preventDefault(); void prepare('website.certificate.bind', {site_id: site.id, certificate_id: certificateId});}}><Field label="绑定证书" hint="证书需覆盖网站的全部域名。"><select required value={certificateId} disabled={busy} onChange={event => setCertificateId(event.target.value)}><option value="">选择证书</option>{data.certificates.map(item => <option key={item.id} value={item.id} disabled={item.status !== 'valid'}>{item.name} · {item.id}{item.status !== 'valid' ? ' · 校验失败' : ''}</option>)}</select></Field><div className="form-actions"><button className="button secondary" disabled={busy || !certificateId || certificateId === site.certificate_id}>预览更换绑定证书</button></div></form>
      <details className="advanced separated"><summary>新增网站</summary><form className="form-stack" onSubmit={event => {event.preventDefault(); void prepare('website.create', {title: newTitle, domain: newDomain, certificate_id: newCertificateId});}}><Field label="网站标题"><input required value={newTitle} maxLength={120} disabled={busy} onChange={event => setNewTitle(event.target.value)} /></Field><Field label="初始域名"><input required value={newDomain} maxLength={253} disabled={busy} onChange={event => setNewDomain(event.target.value)} /></Field><Field label="证书"><select required value={newCertificateId} disabled={busy} onChange={event => setNewCertificateId(event.target.value)}><option value="">选择证书</option>{data.certificates.map(item => <option key={item.id} value={item.id} disabled={item.status !== 'valid'}>{item.name} · {item.id}</option>)}</select></Field><Notice>新网站拥有独立内容和本机 HTTPS 监听。新增网站不会自动切换 REALITY 握手目标；请在代理页另行预览迁移。</Notice><div className="form-actions"><button className="button primary" disabled={busy || !newCertificateId}>预览新增网站</button></div></form></details>
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
        <div className="form-actions"><button className="button secondary" disabled={busy || reading || version.current} onClick={() => void prepare('website.content.restore', {site_id: site.id, version_id: version.id})}>预览恢复此版本</button></div>
      </div>)}</div>
    </Section>
    <CertificateSection certificates={data.certificates} prepare={prepare} busy={busy} />
  </>;
}
