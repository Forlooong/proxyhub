# 管理面板

`0.3.0a1` 在现有单节点中文管理面板中加入单站点内容上传、隔离预览和版本恢复。默认浅色界面、自托管 IBM Plex 字体、可点击资源关系图、变更抽屉和任务记录；深色模式与移动端布局同时提供。设计取舍见 [面板设计记录](../packages/panel/DESIGN.md)，本次真实环境验证见[阶段 2a 验收记录](phase2a-content-acceptance.md)。

## 安装与访问

先按 [CLI 操作手册](runbook.md) 完成最小部署；当前验收平台是 Debian 12 amd64。面板不自动初始化服务器，也不替换原命令。构建机执行：

```sh
npm ci
npm run build
python scripts/package_app.py
```

把归档解包到受管 `/opt/proxyhub/app`。面板 Python 依赖独立安装到 `/opt/proxyhub/panel-python`，不要安装到系统 Python 环境。下面命令使用已有虚拟环境内的 pip；离线部署可先为目标 Python 3.11 / Linux amd64 下载 wheel，然后用 `--no-index --find-links` 安装同一锁文件。

```sh
python -m pip install --target /opt/proxyhub/panel-python -r /opt/proxyhub/app/requirements-panel.lock
proxyhub install-services
proxyhub panel install
proxyhub panel access
```

`install-services` 更新自有单元定义但不重启公网服务。面板安装只启动 `proxyhub-manager.service` 与 `proxyhub-panel.service`。重复安装保留现有登录凭据；碰到非自有账户、单元或路径时拒绝覆盖。

在个人电脑上按 `panel access` 的结果建立 SSH 隧道，再访问 `http://127.0.0.1:18765`。示例：

```sh
ssh -N -L 127.0.0.1:18765:127.0.0.1:18765 -p <SSH_PORT> root@<VPS>
```

使用已有密钥时可加 `-i <PRIVATE_KEY>`。访问凭据由服务器随机生成，仅 root 可读取；可用 `proxyhub panel access --output /root/panel-access.json` 写入私有文件。不要把凭据、节点链接或最终 YAML 放入公开仓库。会话有效期 30 分钟，退出立即撤销当前会话。

## 本阶段操作

| 页面 | 实际可用操作 | 明确边界 |
| --- | --- | --- |
| 总览 | 查看资源关系、当前版本、服务状态、最近任务 | 不显示虚构利用率或流量 |
| 服务器 | 选择账号和连接上下文、读取生效授权路径、预览并追加公钥 | 不关闭密码登录；第二个 SSH 会话需独立验证；sshd 转发策略不代表每把公钥都允许隧道 |
| 网站与证书 | 上传静态网站 ZIP、隔离预览、查看内容版本并恢复；修改默认模板标题、查看证书消费者、受管证书续期、导入证书与匹配私钥 | 单站点，固定网站/订阅两个域名和本机 HTTPS 目标；上传内容时禁用模板标题编辑；证书异常仍可进入修复流程 |
| REALITY | 修改节点名称和客户端连接地址，查看 SNI、内部目标及依赖 | 暂无 HY2、外部目标切换、域名迁移或节点凭据轮换 |
| 订阅 | 修改名称、刷新间隔、手工额度、到期、重置记录和 JSON 配置覆盖；显式查看/复制链接及最终 YAML | 覆盖层经过唯一生成器校验；重置记录不会自动计量或触发归零；没有多订阅归属计费 |
| 任务 | 查看排队、执行、成功、失败、中断记录；恢复未完成部署 | 中断任务不会自动重放输入；只保留最近 200 条完成记录，列表展示最近 30 条 |

订阅元数据与正文共同发布，发布器确认加载新版本后才提交状态。只改订阅时不重启网站和代理；网站、节点和证书变更会在预览中说明服务重载影响。配置版本变化后旧预览不可应用，需要刷新并重新生成。

覆盖层使用受控 JSON 对象编辑，最终 YAML 由生成器产生。`Subscription-Userinfo` 中的到期时间、`Profile-Update-Interval` 和独立 `reset_day/reset_at` 不混用；`reset_at` 持久化为 Unix 秒，表单按浏览器本地时区输入。不同 Clash GUI 的显示兼容性需分别验收。

## 网站内容上传、预览与恢复

在“网站与证书”选择 ZIP，点击“验证并预览上传”。变更抽屉显示文件数、体积与服务影响，点击“加载页面预览”读取隔离预览；确认应用后才切换公网内容。取消预览保留当前部署。内容版本列表展示当前版本、文件数、体积和创建时间，可选择已成功应用的上传版本或默认模板，再预览并确认恢复。未应用或失败的候选不能作为历史版本恢复。

ZIP 压缩体积不得超过 **180 KiB**，实际解压总量不得超过 **2 MiB**，文件和目录合计最多 **200 项**。根目录必须包含非空 UTF-8 `index.html`，该文件不超过 256 KiB。支持的静态扩展名为 `.html`、`.htm`、`.css`、`.js`、`.json`、`.txt`、`.svg`、`.png`、`.jpg`、`.jpeg`、`.gif`、`.webp`、`.ico`、`.woff`、`.woff2`、`.ttf`、`.otf`；不执行上传内容中的服务端代码。路径穿越、隐藏路径、符号链接、特殊文件、重复路径和大小写冲突会被拒绝。

上传和恢复沿用当前域名、证书、REALITY 节点与订阅设置。恢复旧内容不会恢复当时的其他配置；后续订阅、证书和节点变更也会保留已上传的网站内容。内容切换沿用完整部署重载和失败恢复流程，网站、代理与订阅服务可能短暂中断。网站标题输入仅修改默认模板；使用上传内容时，应在本地修改页面后重新打包上传。

隔离预览只读取仍处于准备状态、配置版本未变化的网站计划，生成的 HTML 最多 **512 KiB**。预览禁用脚本、外部资源与页面导航；本地样式表和图片会按支持范围嵌入，依赖 JavaScript 或 CSS `url()` 的效果可能无法完整呈现。预览使用无权限的 sandbox iframe，不授予脚本或同源权限。公网网站允许加载上传包中的同源外置 `.js`，但不放行内联脚本、HTML 事件处理脚本或外部 CDN 等资源；页面应按此约束构建。

CLI 使用同一操作层，需要 Linux root。上传和恢复命令只创建计划，输出中的 `plan_id` 用于预览和显式应用；内容版本 ID 从 `versions` 列表读取：

```sh
proxyhub site content upload --archive /root/website.zip
proxyhub site content preview --plan-id <PLAN_ID> --output /root/website-preview.html
proxyhub apply <PLAN_ID>

proxyhub site content versions
proxyhub site content restore --version <CONTENT_VERSION_ID>
# 恢复默认模板时使用：proxyhub site content restore --version template
# 恢复命令返回新计划，按上述 preview / apply 流程确认。
```

`preview --output` 写入权限为 `0600` 的本地 HTML 文件，不将 HTML 回显到终端。该文件属于临时预览产物，不应加入仓库；查看后按需删除。预览超限时不会自动放宽隔离规则。配置版本变化后应重新准备计划。

本增量只覆盖单站点内容生命周期。多站点、域名迁移、外部 REALITY 目标、HY2、MRS 规则流水线和自动计量仍属后续范围。

## 权限与排障

网关使用无登录权限的 `proxyhub-panel` 账户；只能读取静态资源、网关配置并调用本地 Unix socket。服务器私钥、节点凭据和状态库不能由网关直接读取。root 管理器只接受列出的类型化操作，通过计划和显式应用执行。请求要求同源、登录会话与 CSRF token；不提供任意 shell、路径或服务名入口。

```sh
proxyhub help panel
proxyhub panel request --operation overview
systemctl status proxyhub-manager proxyhub-panel
journalctl -u proxyhub-manager -u proxyhub-panel --since '10 minutes ago'
```

面板停止不会停止 Nginx、代理或只读订阅发布器。公网服务继续使用最后有效版本。凭据遗失时以 root 再运行 `proxyhub panel access`；请勿直接修改凭据文件造成 hash 不一致。当前没有面板内凭据轮换功能。

需要关闭管理入口时：

```sh
systemctl stop proxyhub-panel proxyhub-manager
```

旧的 `proxyhub host/site/deployment/sub/apply` 和原项目命令继续保留。面板层尚未覆盖的操作继续使用 CLI，跨入口共享配置版本检查，避免旧计划覆盖新变更。
