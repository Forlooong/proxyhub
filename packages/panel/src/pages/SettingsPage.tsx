import type { Overview, Theme } from '../types';
import { Command, Notice, Section, Status } from '../components/common';
import { Icon } from '../components/Icon';

export function SettingsPage({data, theme, setTheme, notify}: {data: Overview; theme: Theme; setTheme: (theme: Theme) => void; notify: (message: string) => void}) {
  return <>
    <Section title="界面外观" description="外观偏好只保存在当前浏览器。"><div className="theme-choices"><button className={`theme-choice ${theme === 'light' ? 'selected' : ''}`} aria-pressed={theme === 'light'} onClick={() => setTheme('light')}><Icon name="sun" /><strong>浅色</strong><span>明亮环境下保持清晰</span></button><button className={`theme-choice ${theme === 'dark' ? 'selected' : ''}`} aria-pressed={theme === 'dark'} onClick={() => setTheme('dark')}><Icon name="moon" /><strong>深色</strong><span>降低夜间界面亮度</span></button></div></Section>
    <Section title="统一命令入口" description="面板与命令行共用操作与校验。现有项目和原命令保留。"><Command text="proxyhub help" notify={notify} /><Command text="proxyhub help panel" notify={notify} /><Command text="proxyhub help host append-key" notify={notify} /><Command text="proxyhub help deployment" notify={notify} /><p className="muted">每个页面顶部都能复制对应模块的帮助命令。</p></Section>
    <Section title="当前能力" description="以已安装版本报告为准，后续能力不会显示为可执行操作。"><div className="capability-list">{data.capabilities.map(capability => <div key={capability.name}><div><strong>{capability.name}</strong><p>{capability.reason}</p></div><Status value={capability.available ? 'active' : 'not_checked'} /></div>)}</div><Notice>当前支持多网站、证书依赖、REALITY 入口迁移、HY2 和能力矩阵内的独立协议节点。规则流水线与自动流量计量按后续阶段实施。</Notice></Section>
    <Section title="操作方式"><div className="help-grid"><div><h3>先预览，再应用</h3><p>检查变更抽屉中的当前值、目标值与影响范围。配置已变化时，需要重新预览。</p></div><div><h3>敏感内容按需显示</h3><p>订阅链接与 YAML 需手动显示。管理凭据和私钥不会进入浏览器存储。</p></div><div><h3>公钥写入后验证登录</h3><p>保持当前 SSH 会话，另开会话验证新公钥；面板不会据此自动收紧登录策略。</p></div><div><h3>快速导航</h3><p><kbd>Ctrl</kbd> / <kbd>⌘</kbd> + <kbd>K</kbd> 打开命令菜单，<kbd>Esc</kbd> 关闭。所有操作支持键盘。</p></div></div></Section>
  </>;
}
