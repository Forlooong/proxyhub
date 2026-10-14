import { useEffect, useState } from 'react';
import { login, message } from '../api';
import { Field, Notice } from './common';
import { Icon } from './Icon';

export function Login({onLogin, initialError, blocked = false}: {onLogin: () => void; initialError: string; blocked?: boolean}) {
  const [credential, setCredential] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);
  useEffect(() => {setError(initialError);}, [initialError]);
  async function submit() {
    if (blocked) return;
    const secret = credential; setCredential(''); setError(''); setBusy(true);
    try {await login(secret); onLogin();} catch (error) {setError(message(error));} finally {setBusy(false);}
  }
  return <main className="login-layout"><div className="login-story"><div className="brand"><img src="/favicon.svg" alt="" /><span>ProxyHub</span></div><h1>你的服务，<br />清晰可控。</h1><p>网站、证书、代理与订阅，<br />沿着一条可追溯的路径管理。</p><div className="login-path" aria-hidden="true"><span><Icon name="site" />网站</span><i /><span><Icon name="proxy" />REALITY</span><i /><span><Icon name="subscription" />订阅</span></div><small>独立管理 · 保留原命令</small></div><section className="login-panel"><div className="login-icon"><Icon name="lock" /></div><h2>连接管理面板</h2><p className="muted">通过 SSH 隧道访问，使用服务器上生成的管理凭据登录。</p>{error && <Notice tone="error">{error}</Notice>}<form className="form-stack" onSubmit={event => {event.preventDefault(); void submit();}}><Field label="管理凭据"><input autoFocus type="password" value={credential} onChange={event => setCredential(event.target.value)} required autoComplete="off" maxLength={512} disabled={busy || blocked} placeholder="粘贴管理凭据" /></Field><button className="button primary" disabled={busy || blocked} type="submit">{blocked ? '正在确认退出…' : busy ? '正在验证…' : '登录面板'}<Icon name="arrow" /></button></form><p className="login-note"><Icon name="lock" />凭据只用于本次登录，不写入浏览器存储。</p></section></main>;
}
