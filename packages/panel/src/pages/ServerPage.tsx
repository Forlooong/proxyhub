import { useState } from 'react';
import { message, operation } from '../api';
import type { FormProps, SSHContext } from '../types';
import { Details, Field, formatBytes, Notice, Section } from '../components/common';
import { Icon } from '../components/Icon';

type Inspection = {account: string; paths: {id: string; label: string}[]; public_key_authentication: boolean | string; tunnel_policy_allowed?: boolean | null; tunnel_verification?: string; tunnel_note?: string};
export function ServerPage({data, prepare, busy}: FormProps) {
  const [account, setAccount] = useState(data.server.accounts[0] ?? '');
  const [context, setContext] = useState<SSHContext>(data.server.ssh_context);
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [pathId, setPathId] = useState('');
  const [key, setKey] = useState('');
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState('');
  const disabled = busy || checking;
  const invalidate = () => {setInspection(null); setPathId(''); setError('');};
  async function inspect() {
    setChecking(true); setError(''); setInspection(null);
    try {
      const result = await operation<Inspection>('ssh.inspect', {account, context});
      setInspection(result); setPathId(result.paths.length === 1 ? result.paths[0]!.id : '');
    } catch (error) {setError(message(error));} finally {setChecking(false);}
  }
  async function readKey(file?: File) {
    if (!file) return;
    if (file.size > 16384) {setError('公钥文件过大，请选择单个 SSH 公钥文件。'); return;}
    try {setKey(await file.text()); setError('');} catch {setError('无法读取公钥文件，请重新选择或粘贴公钥。');}
  }
  return <>
    <Section title={data.server.hostname} description="服务器环境与登录入口"><Details rows={[
      ['操作系统', data.server.os], ['架构', data.server.architecture], ['物理内存', formatBytes(data.server.memory_total_bytes)], ['当前 SSH 端口', String(context.lport)],
    ]} /></Section>
    <Section title="追加已有 SSH 公钥" description="先检查账号的实际授权路径，再预览指纹与追加结果。">
      <Notice>保留原有授权项。写入后仍需在第二个 SSH 会话验证新公钥登录，面板不会关闭密码登录。</Notice>
      <form className="form-stack" onSubmit={event => {event.preventDefault(); void inspect();}}>
        <Field label="目标账号"><select value={account} disabled={disabled} onChange={event => {setAccount(event.target.value); invalidate();}} required><option value="" disabled>选择账号</option>{data.server.accounts.map(name => <option key={name}>{name}</option>)}</select></Field>
        <details className="advanced"><summary>SSH 连接上下文</summary><p className="muted">用于核对 sshd 的 Match 条件。请填写将使用公钥连接的来源与目标。</p><div className="form-grid">
          {([['addr', '客户端来源 IP'], ['host', '客户端主机名'], ['laddr', '服务器接收地址'], ['lport', '服务器 SSH 端口']] as const).map(([name, label]) => <Field key={name} label={label}><input value={context[name]} disabled={disabled} required onChange={event => {setContext({...context, [name]: event.target.value}); invalidate();}} inputMode={name === 'lport' ? 'numeric' : 'text'} /></Field>)}
        </div></details>
        <div><button className="button secondary" type="submit" disabled={disabled}><Icon name="search" />{checking ? '检查中…' : '检查授权路径'}</button></div>
      </form>
      {error && <Notice tone="error">{error}</Notice>}
      {inspection && <form className="form-stack separated" onSubmit={event => {event.preventDefault(); void prepare('ssh.append', {account, context, public_key: key.replace(/\r?\n$/, ''), path_id: pathId});}}>
        <div className="inline-info">公钥认证：<strong>{inspection.public_key_authentication === true || inspection.public_key_authentication === 'yes' ? '已启用' : '未启用'}</strong></div>
        <Notice>sshd 转发策略：{inspection.tunnel_policy_allowed === true ? '允许' : inspection.tunnel_policy_allowed === false ? '禁止' : '未知'}。{inspection.tunnel_note ?? '密钥限制与实际隧道仍需通过独立 SSH 会话验证。'}</Notice>
        <Field label="授权文件" hint="路径来自当前生效的 SSH 配置，不能任意指定服务器文件。"><select required disabled={disabled} value={pathId} onChange={event => setPathId(event.target.value)}><option value="" disabled>选择授权文件</option>{inspection.paths.map(path => <option key={path.id} value={path.id}>{path.label}</option>)}</select></Field>
        <Field label="SSH 公钥" hint="支持带限制选项的单个授权项。无需上传私钥。"><textarea className="code-input" required rows={4} maxLength={16384} placeholder="ssh-ed25519 AAAA… your-device" value={key} disabled={disabled} onChange={event => setKey(event.target.value)} spellCheck={false} /></Field>
        <label className="file-picker">或读取公钥文件<input type="file" accept=".pub,text/plain" disabled={disabled} onChange={event => {void readKey(event.target.files?.[0]); event.target.value = '';}} /></label>
        <div className="form-actions"><button className="button primary" disabled={disabled || !pathId} type="submit"><Icon name="key" />预览公钥追加</button></div>
      </form>}
    </Section>
  </>;
}
