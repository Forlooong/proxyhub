# 管理面板

`0.4.0a1` 在现有单节点中文管理面板中加入多站点、域名绑定和证书依赖管理，保留内容上传、隔离预览和逐站版本恢复。默认浅色界面、自托管 IBM Plex 字体、可点击资源关系图、变更抽屉和任务记录；深色模式与移动端布局同时提供。设计取舍见 [面板设计记录](../packages/panel/DESIGN.md)，此前单站点内容功能的验证见[阶段 2a 验收记录](phase2a-content-acceptance.md)。

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

**从阶段 2a 升级后，须先显式激活一次 Web 配置，再申请新增域名的 HTTP-01 证书。** 此次激活以 `web_only` 归一化旧配置，保留默认网站及内容历史，并加载新的 TCP 80 默认挑战入口；仅更新应用文件或重启面板不会生效。本次部署按此顺序执行。手动升级可先列出资源，再为默认网站重新绑定其当前证书并应用返回的计划：

```sh
proxyhub site website list
proxyhub site website certificate bind --site-id default --certificate-id <CURRENT_CERTIFICATE_ID>
proxyhub apply <PLAN_ID>
```

纯阶段 2a 配置归一化后的当前证书 ID 为 `default`。该操作保留网站内容、原 SNI 与节点凭据，只重载网站和订阅发布器。新入口仅对 `/.well-known/acme-challenge/` 提供 GET/HEAD 文件验证，其他未知域名 HTTP 路径仍拒绝，未知 TLS SNI 仍拒绝。

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
| 网站与证书 | 创建、选择、启用、停用和删除网站，绑定/解绑域名，更换证书绑定；逐站上传 ZIP、隔离预览和恢复内容；申请、导入、替换、续期和删除证书，查看消费者 | 默认网站及原 REALITY SNI 受依赖保护；已有内部监听不变；上传内容时禁用模板标题编辑；证书异常仍可进入修复流程 |
| REALITY | 修改节点名称和客户端连接地址，查看 SNI、内部目标及依赖 | 暂无 HY2、外部目标切换、域名迁移或节点凭据轮换 |
| 订阅 | 修改名称、刷新间隔、手工额度、到期、重置记录和 JSON 配置覆盖；显式查看/复制链接及最终 YAML | 覆盖层经过唯一生成器校验；重置记录不会自动计量或触发归零；没有多订阅归属计费 |
| 任务 | 查看排队、执行、成功、失败、中断记录；恢复未完成部署 | 中断任务不会自动重放输入；只保留最近 200 条完成记录，列表展示最近 30 条 |

订阅元数据与正文共同发布，发布器确认加载新版本后才提交状态。只改订阅时不重启网站和代理；网站、节点和证书变更会在预览中说明服务重载影响。配置版本变化后旧预览不可应用，需要刷新并重新生成。

覆盖层使用受控 JSON 对象编辑，最终 YAML 由生成器产生。`Subscription-Userinfo` 中的到期时间、`Profile-Update-Interval` 和独立 `reset_day/reset_at` 不混用；`reset_at` 持久化为 Unix 秒，表单按浏览器本地时区输入。不同 Clash GUI 的显示兼容性需分别验收。

## 多站点与证书依赖

“当前管理的网站”只切换表单与内容历史的操作对象，不改变 REALITY 目标。网站、证书绑定和内部监听各有稳定编号；原网站及初始共享证书为 `default`，新增资源使用 32 位十六进制 ID。标题、域名和证书指纹不作为身份；替换证书材料保留绑定 ID。每个网站有独立内容目录与固定内部 HTTPS 监听，最多 16 个网站、32 个证书绑定，每个网站或证书最多 10 个域名。

新增网站前先申请 HTTP-01 证书或导入覆盖其域名的证书，再在“新增网站”填写标题、初始域名和证书。创建、启用及绑定域名会检查全部 A/AAAA 是否指向本机、实际证书 SAN、SNI 和监听冲突。默认网站可添加别名；原 SNI 继续经过现有 REALITY，其他网站域名和别名直接进入对应网站 HTTPS。此处不能切换已有节点的 SNI 或握手目标。

停用网站保留内容、域名和证书绑定；删除网站须先停用。启用网站必须有域名和有效证书，解绑最后一个域名前也须先停用。默认网站及其原 SNI 被 REALITY 引用，不能停用、删除或解绑。证书列表显示网站 HTTPS、订阅 HTTPS、REALITY 间接依赖及停用网站保留的绑定；仍被引用的证书不能删除，须先解除或迁移引用。

证书申请使用现有 HTTP-01 入口，要求公网 TCP 80 可达；新增域名先完成上述升级激活。受管签发按证书绑定分别维护续期来源；手工导入默认由原签发方维护。替换或续期会校验证书与私钥、全部依赖域名和有效期，重载后检查实际消费者的指纹与网站内容；失败恢复旧部署，未完成恢复时保留待恢复状态。

CLI 与面板共用计划和依赖检查。以下示例域名须替换为自己的域名；除列表外，每条命令分别返回计划，检查后执行 `proxyhub apply <PLAN_ID>`。新增证书应用成功后，从列表读取其 ID 再创建网站：

```sh
proxyhub site certificate list
proxyhub site certificate request --name "新站点证书" --domain second.example.test
proxyhub site certificate add --name "手工证书" --domain second.example.test \
  --certificate /root/fullchain.pem --key /root/privkey.pem
proxyhub site certificate replace --certificate-id <CERTIFICATE_ID> \
  --certificate /root/fullchain.pem --key /root/privkey.pem
proxyhub site certificate renew --certificate-id <CERTIFICATE_ID>
proxyhub site certificate delete --certificate-id <UNREFERENCED_CERTIFICATE_ID>

proxyhub site website list
proxyhub site website create --title "第二网站" --domain second.example.test --certificate-id <CERTIFICATE_ID>
proxyhub site website domain bind --site-id <SITE_ID> --domain alias.example.test
proxyhub site website domain unbind --site-id <SITE_ID> --domain alias.example.test
proxyhub site website certificate bind --site-id <SITE_ID> --certificate-id <CERTIFICATE_ID>
proxyhub site website disable --site-id <SITE_ID>
proxyhub site website enable --site-id <SITE_ID>
proxyhub site website delete --site-id <DISABLED_SITE_ID>
```

原 `proxyhub site certificate issue/import` 仍保留为初始签发和材料导入入口；已部署多站点应使用上面的 `request/add/replace` 管理稳定证书绑定。

## 网站内容上传、预览与恢复

在“网站与证书”先选择当前管理的网站，再选择 ZIP，点击“验证并预览上传”。变更抽屉显示文件数、体积与服务影响，点击“加载页面预览”读取隔离预览；确认应用后才切换该网站公网内容。取消预览保留当前部署。内容版本列表仅展示所选网站的当前版本、文件数、体积和创建时间，可选择已成功应用的上传版本或默认模板，再预览并确认恢复。未应用、失败或属于其他网站的候选不能恢复到当前网站；阶段 2a 的既有历史继续归属默认网站。

ZIP 压缩体积不得超过 **180 KiB**，实际解压总量不得超过 **2 MiB**，文件和目录合计最多 **200 项**。根目录必须包含非空 UTF-8 `index.html`，该文件不超过 256 KiB。支持的静态扩展名为 `.html`、`.htm`、`.css`、`.js`、`.json`、`.txt`、`.svg`、`.png`、`.jpg`、`.jpeg`、`.gif`、`.webp`、`.ico`、`.woff`、`.woff2`、`.ttf`、`.otf`；不执行上传内容中的服务端代码。路径穿越、隐藏路径、符号链接、特殊文件、重复路径和大小写冲突会被拒绝。

上传和恢复沿用当前域名、证书、REALITY 节点与订阅设置。恢复旧内容不会恢复当时的其他配置；后续订阅、证书和节点变更也会保留各站点已上传的内容。网站与证书变更使用 Web 激活和失败恢复流程，重载 Nginx 与订阅发布器，保留代理进程。网站标题输入仅修改所选网站的默认模板；使用上传内容时，应在本地修改页面后重新打包上传。

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

# 不传 --site-id 时仍操作默认网站；指定网站时使用稳定 ID。
proxyhub site content upload --site-id <SITE_ID> --archive /root/second-website.zip
proxyhub site content versions --site-id <SITE_ID>
proxyhub site content restore --site-id <SITE_ID> --version <CONTENT_VERSION_ID>
```

`preview --output` 写入权限为 `0600` 的本地 HTML 文件，不将 HTML 回显到终端。该文件属于临时预览产物，不应加入仓库；查看后按需删除。预览超限时不会自动放宽隔离规则。配置版本变化后应重新准备计划。

阶段 2b 覆盖多站点与证书依赖。已有节点的 SNI 迁移和 REALITY 目标切换归阶段 2c；HY2 与协议扩展归阶段 2d。MRS 规则流水线和自动计量仍属后续范围。

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
