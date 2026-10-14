import { useState } from 'react';
import { navigation } from '../navigation';
import type { Page } from '../types';
import { Dialog } from './Dialog';
import { Icon } from './Icon';

export function CommandMenu({close, navigate, refresh}: {close: () => void; navigate: (page: Page) => void; refresh: () => void}) {
  const [query, setQuery] = useState('');
  const commands = [...navigation.map(item => ({...item, action: () => navigate(item.id)})), {id: 'refresh', label: '刷新当前状态', description: '从服务器重新读取数据', action: refresh}];
  const filtered = commands.filter(item => `${item.label} ${item.description}`.includes(query));
  return <Dialog title="快速前往" onClose={close} className="command-dialog"><label className="search-input"><Icon name="search" /><input autoFocus placeholder="搜索页面或操作" aria-label="搜索页面或操作" value={query} onChange={event => setQuery(event.target.value)} /></label><div className="command-list">{filtered.map(item => <button key={item.id} onClick={() => {item.action(); close();}}><Icon name={item.id === 'refresh' ? 'refresh' : item.id as Page} /><span><strong>{item.label}</strong><small>{item.description}</small></span><Icon name="arrow" /></button>)}{!filtered.length && <p className="empty">没有匹配项，试试“网站”或“订阅”。</p>}</div><div className="command-footer"><kbd>Tab</kbd> 选择 <kbd>Enter</kbd> 打开 <kbd>Esc</kbd> 关闭</div></Dialog>;
}
