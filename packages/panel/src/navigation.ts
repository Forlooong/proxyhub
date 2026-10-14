import type { Page } from './types';
export const navigation: {id: Page; label: string; description: string}[] = [
  {id: 'overview', label: '总览', description: '查看服务关系与当前状态'},
  {id: 'server', label: '服务器', description: '管理账号与 SSH 公钥'},
  {id: 'site', label: '网站与证书', description: '维护网站标题与 TLS 证书'},
  {id: 'proxy', label: '代理服务', description: '管理 REALITY 节点'},
  {id: 'subscription', label: '订阅管理', description: '配置、显示信息与客户端链接'},
  {id: 'jobs', label: '任务记录', description: '检查变更结果与失败原因'},
  {id: 'settings', label: '设置与帮助', description: '外观、命令与能力边界'},
];
export const pageCommands: Record<Page, string> = {
  overview: 'proxyhub help', server: 'proxyhub help host append-key', site: 'proxyhub help site certificate',
  proxy: 'proxyhub help deployment', subscription: 'proxyhub help sub', jobs: 'proxyhub help panel', settings: 'proxyhub help',
};
