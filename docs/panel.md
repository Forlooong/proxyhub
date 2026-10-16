# 管理面板

`0.7.1a1` 增加固定共享运行组：两个 REALITY 进程继续独立，HY2 与七类额外节点共享一个进程，资源身份、监听端口、凭据及依赖不变。`0.7.0a1` 的阶段 2 功能验收继续作为历史记录；本轮已部署至专用 VPS 并完成执行自测，等待独立审计，不能表述为审计已放行。实测、失败记录与剩余边界见[共享组执行交付](phase2-protocols-acceptance.md#2026-10-16-固定共享运行组执行交付)。界面保留浅/深色、自托管字体、资源关系图、变更抽屉和移动布局；设计取舍见[面板设计记录](../packages/panel/DESIGN.md)。

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

`install-services` 更新自有单元定义但不重启公网服务。升级到 `0.7.1a1` 后也须执行，以安装 `proxyhub-shared.service`；旧 REALITY、HY2 和 `proxyhub-node@.service` 单元保留。该命令不迁移旧运行布局，新入口及布局只在显式应用对应计划时生效。面板安装只启动管理器与网关，重复安装保留登录凭据；碰到非自有账户、单元或路径时拒绝覆盖。

**从 `0.6.0a1` 升级时，须在首次应用额外协议前重启一次发布器。** 常驻 Python 进程仅 reload 不会加载新校验代码；先在旧有效 bundle 仍为当前版本时加载支持额外 URI 的校验器，再应用新节点。更早版本加入 HY2 时也有同样要求。管理器与面板同步更新代码，原 REALITY/HY2 与 Nginx 保留，不得记为全部 PID 全程不变：

```sh
systemctl restart proxyhub-publisher.service
systemctl restart proxyhub-manager.service proxyhub-panel.service
```

上述是代码升级步骤。后续 HY2 或额外节点操作对发布器只执行 reload；共享布局中的代理进程是否重启，取决于整个共享组运行配置是否变化，具体影响如下。

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

## 固定共享运行组与旧布局迁移

共享布局使用一个 `proxyhub-shared.service` 读取当前不可变 revision 的 `shared.json`，承载所有启用的 HY2 与额外节点。两个 REALITY 进程、Nginx 和订阅发布器保持独立。节点的稳定身份、端口、认证、证书绑定、网站、订阅入口和原命令不因迁移改变。旧布局普通节点操作仍沿用旧布局；升级文件、安装单元或重启面板不会隐式迁移。

旧布局需 Linux root 显式准备专用迁移计划，核对返回的 `shared_runtime` 成员、配置变化和重连影响，再应用返回的计划编号：

```sh
proxyhub install-services
proxyhub deployment migrate-runtime
proxyhub apply <PLAN_ID>
# 激活中断后，检查任务与 pending 状态，再使用原恢复命令。
proxyhub deployment recover
```

迁移复用现有渲染、计划、事务、唯一生成器和恢复入口。切换前以隔离端口认证候选组；正式切换后检查 HY2 与所有启用额外节点，证书成员分别验证可信链/名称及选定叶指纹。旧 `proxyhub-hy2.service`、`proxyhub-node@.service`、单节点配置和旧 revision 保留，用于激活失败或进程中断后恢复原布局。恢复失败保留 pending，不把启动成功等同真实公网验收。

共享组内一个启用入站的端口、传输或证书材料变化，以及启停导致组配置变化，都会切换共享组；HY2 与全部已启用额外节点的现有连接均需重连，即使其中某个成员本身未修改。组级预览列出影响成员。名称、订阅元数据及停用资源的操作若未改变共享运行配置，则不重启该组；最后一个启用成员停用后停止组，资源身份、凭据与依赖仍保留。总览列出真实运行单元，资源停用状态独立于共享服务状态。SOCKS5 关闭 UDP 的服务端拒绝规则带对应 `inbound` 条件，不扩散到共享组其他入站；客户端默认 `MATCH,REJECT` 边界仍按下文执行。

本次资源调整已部署并完成执行自测，停在交付点等待独立审计，不自行进入阶段 3a。当前负载工作预算为 RSS ≤325 MiB，原 RSS <350 MiB、峰值 <650 MiB、无 OOM 指标不变；迁移后实测与采样口径见[执行记录及 JSON 证据](phase2-protocols-acceptance.md#2026-10-16-固定共享运行组执行交付)。约 311.2 MiB 的前置评估仍属于隔离估算，首次迁移全过程峰值、首轮故障恢复未完成的根因、本轮个人电脑外网复验、真实浏览器/指定 GUI 及正式 1 核 1GB、5 订阅/500 节点 PERF-1 均未验证。

## 本阶段操作

| 页面 | 实际可用操作 | 明确边界 |
| --- | --- | --- |
| 总览 | 查看资源关系、当前版本、服务状态、最近任务 | 不显示虚构利用率或流量 |
| 服务器 | 选择账号和连接上下文、读取生效授权路径、预览并追加公钥 | 不关闭密码登录；第二个 SSH 会话需独立验证；sshd 转发策略不代表每把公钥都允许隧道 |
| 网站与证书 | 创建、选择、启用、停用和删除网站，绑定/解绑域名，更换证书绑定；逐站上传 ZIP、隔离预览和恢复内容；申请、导入、替换、续期和删除证书，查看消费者 | 所有存续 REALITY 入口、HY2 与独立节点（包括停用资源）的直接或间接证书依赖均参与保护；默认网站按旧配置兼容保留；已有内部监听不变；上传内容时禁用模板标题编辑；证书异常仍可进入修复流程 |
| 代理服务 | 修改 REALITY 名称与连接地址，迁移 SNI、切回或退役保留入口；创建、更新、启用或停用 HY2；创建、更新、启停和删除七种协议的独立节点，选择证书或 REALITY 目标 | 独立节点最多 32 个，须先停用再删除；协议与凭据不可修改；[传输与 H2 UDP 边界](phase2-protocols-acceptance.md)以验收记录为准；外部 REALITY 目标仅 HTTPS 443，主入口或最后一个入口不能退役；HY2 仅一个独立 IPv4 UDP 资源，不支持删除或高级参数 |
| 订阅 | 修改名称、刷新间隔、手工额度、到期、重置记录和 JSON 配置覆盖；显式查看/复制链接及最终 YAML | 覆盖层经过唯一生成器校验；重置记录不会自动计量或触发归零；没有多订阅归属计费 |
| 任务 | 查看排队、执行、成功、失败、中断记录；恢复未完成部署 | 中断任务不会自动重放输入；只保留最近 200 条完成记录，列表展示最近 30 条 |

订阅元数据与正文共同发布，发布器确认加载新版本后才提交状态。只改订阅时不重启网站和代理，也不依赖外部握手目标当前可达；网站、节点和证书变更会在预览中说明服务重载影响。配置版本变化后旧预览不可应用，需要刷新并重新生成。

覆盖层使用受控 JSON 对象编辑，最终 YAML 由生成器产生。`Subscription-Userinfo` 中的到期时间、`Profile-Update-Interval` 和独立 `reset_day/reset_at` 不混用；`reset_at` 持久化为 Unix 秒，表单按浏览器本地时区输入。不同 Clash GUI 的显示兼容性需分别验收。

## 多站点与证书依赖

“当前管理的网站”只切换表单与内容历史的操作对象，不改变 REALITY 目标。网站、证书绑定和内部监听各有稳定编号；原网站及初始共享证书为 `default`，新增资源使用 32 位十六进制 ID。标题、域名和证书指纹不作为身份；替换证书材料保留绑定 ID。每个网站有独立内容目录与固定内部 HTTPS 监听，最多 16 个网站、32 个证书绑定，每个网站或证书最多 10 个域名。

新增网站前先申请 HTTP-01 证书或导入覆盖其域名的证书，再在“新增网站”填写标题、初始域名和证书。创建、启用及绑定域名会检查全部 A/AAAA 是否指向本机、实际证书 SAN、SNI 和监听冲突。默认网站可添加别名；受管域名被存续 REALITY 入口使用时先进入该入口，否则直接进入对应网站 HTTPS。网站选择仅改变管理上下文，节点 SNI 与握手目标迁移在 REALITY 页操作。

停用网站保留内容、域名和证书绑定；删除网站须先停用。启用网站必须有域名和有效证书，解绑最后一个域名前也须先停用。所有存续 REALITY 入口，包括未被订阅发布的旧入口，均保护其目标网站、SNI 和证书依赖；切换主入口不会释放旧入口依赖，须先显式退役对应入口。默认网站及其首域名另因旧配置兼容继续保留，即使原 REALITY 入口已退役也不能停用、删除或解绑。证书列表显示网站 HTTPS、订阅 HTTPS、各 REALITY 入口间接依赖及 HY2；停用网站或 HY2 仍保留证书绑定，仍被引用的证书不能删除，须先解除或迁移引用。

证书申请使用现有 HTTP-01 入口，要求公网 TCP 80 可达；新增域名先完成上述升级激活。受管签发按证书绑定分别维护续期来源；手工导入默认由原签发方维护。替换或续期会校验证书与私钥、全部依赖域名和有效期，重载后检查实际消费者的指纹与网站内容。已启用 HY2 的证书材料路径变化时，仅重启 HY2 代理实例，保留 REALITY 进程；另按既有 Web 流程重载网站和订阅 HTTPS。HY2 分别通过普通可信链/名称验证与指定叶证书指纹验证，不能用 pin 代替 PKI 信任；失败恢复旧部署，未完成恢复时保留待恢复状态。

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

## REALITY 目标切换与 SNI 迁移

REALITY 页列出当前主入口与全部保留入口，包括稳定入口编号、内部监听、目标地址、SNI、网站及证书依赖。每个入口拥有独立监听和目标；节点 UUID、公私钥与 short ID 继续沿用，不创建另一套节点凭据。最多保留 8 个入口；同一 SNI 已有入口时，应选择该入口，不能重复迁移创建。

在“迁移到新目标”中选择目标类型。**本机受管网站**只能选择已启用的网站及其已绑定域名；目标使用该网站的内部 HTTPS 地址，避免绕回公网监听。**外部 TLS 网站**将连接地址与 SNI 分开填写：地址填写外部域名，端口固定为 HTTPS 443，SNI 必须由目标证书覆盖。系统检查全部 A/AAAA 地址，拒绝私网、回环、特殊地址及指向本机的目标，并固定一个已验证的公网 IP；后续检查要求该固定 IP 仍属于目标的 DNS 结果，防止地址漂移绕回本机。

预览与应用使用有超时限制的 TLS 探测，检查目标的 TLS 1.3、X25519、SNI 与可信证书链。探测属于候选准入检查；它不能代替真实 REALITY 客户端验收，也不表示保留入口的远端目标始终健康。保留外部入口仍依赖该外部服务后续的证书、DNS 和可用性。

检查变更抽屉后确认应用，系统启动新入口，重载公网 SNI 路由并检查将要发布的入口，再把当前主入口发布到订阅；未改动的旧入口配置及实例保持原状，既有外部目标故障不阻断切回可用入口。迁移期间旧入口、旧代理进程、现有网站和订阅服务保留。客户端刷新订阅后使用新 SNI，尚未刷新的客户端继续使用旧入口；请分别验证旧、新客户端的真实代理请求。候选或恢复检查失败时不提交新配置，未完成恢复的部署须先恢复后再生成其他计划。

选择“预览切为主入口”可以重新发布一个保留入口，无需替换节点凭据或重启入口进程。切回主入口仍保留其他入口。确认旧客户端已刷新订阅后，再通过“预览退役入口”单独确认退役；退役会移除该入口的路由并停止其服务，继续使用旧 SNI 的 REALITY 客户端将失去该入口。当前主入口及最后一个入口不能退役。默认网站仍按上文的兼容约束保留，退役旧 REALITY 入口不会删除网站或原命令。

CLI 与面板共用检查和显式计划。`list` 只读；其余命令先返回计划，检查后分别执行 `proxyhub apply <PLAN_ID>`。本机与外部目标参数互斥，`--sni` 必填，外部端口固定为 443：

```sh
proxyhub proxy reality list
proxyhub proxy reality migrate --site-id <ENABLED_SITE_ID> --sni second.example.test
proxyhub apply <MIGRATION_PLAN_ID>

proxyhub proxy reality migrate --external-host external.example.test --sni tls.example.test
proxyhub apply <MIGRATION_PLAN_ID>

proxyhub proxy reality select --endpoint-id <RETAINED_ENDPOINT_ID>
proxyhub apply <SELECTION_PLAN_ID>

proxyhub proxy reality retire --endpoint-id <NONPRIMARY_ENDPOINT_ID>
proxyhub apply <RETIREMENT_PLAN_ID>
```

入口编号从 `list` 读取；初始兼容入口编号为 `default`。外部目标地址与 SNI 可以不同，但须实际通过上述网络与 TLS 检查；示例中的域名不是可直接应用的测试目标。订阅名称、额度等元数据更新继续使用原订阅操作，不要求外部目标在线。

## HY2 生命周期

在“代理服务”的 HY2 表单中填写名称、UDP 端口、SNI 和证书绑定，再预览并应用。只支持一个本机 HY2 资源，稳定编号为 `local-hy2`、监听编号为 `hy2-default`；默认名称为 `ProxyHub HY2`、端口为 UDP 443。名称不能与 REALITY 或保留分组重名。服务独立监听 `0.0.0.0`，与 Nginx TCP 443 共存；当前不启用 IPv6 HY2 监听或网站 HTTP/3。端口占用在准备与应用时检查，网络和云安全组须允许实际 UDP 端口；操作不自动修改防火墙。

SNI 必须由所选可信证书覆盖，应用会校验实际证书、私钥、SAN 和有效期；不要求该名称已绑定网站，也不要求对应网站启用。密码在首次创建时随机生成，名称、端口、SNI、证书更新及停用/重新启用均保留密码。普通资源列表和变更预览不返回密码，最终订阅属于含凭据材料。当前没有密码轮换、删除、obfs、带宽、端口跳跃或其他高级参数。

受管证书归档目录继续为 root 专有 `0700`，私钥文件保持 `0600`，不为 HY2 放宽归档权限。部署把所需 PEM 嵌入 `0640 root:proxyhub-proxy` 的候选运行配置；共享布局由低权限共享组读取 `shared.json`，原 `hy2.json` 保留校验与恢复用途。实例不直接读取 root 专有证书归档。这些配置包含密码与私钥，应按秘密材料保护。

HY2 创建、更新和启停仍使用类型化计划。共享布局下运行配置变化会切换 `proxyhub-shared.service`，HY2 与全部已启用额外节点的现有连接需重连；未迁移的旧布局仍调整原 `proxyhub-hy2.service`。网站、REALITY 新旧入口和原命令保留。停用停止该入站监听并从订阅移除 HY2，但保留其配置、凭据和证书依赖；重新启用恢复同一资源。唯一生成器仍按 REALITY、启用 HY2、启用额外节点顺序生成，`PROXY` 手选组仍以 REALITY 为首选。`raw` 为该有序 URI 列表的 Base64；兼容字段 `uri` 仍表示 REALITY 单条 URI，`uris` 为有序列表。

CLI 与面板使用相同计划；`list` 只读，其余操作各自返回计划，检查后分别显式应用。创建默认立即启用，更新只接受名称、UDP 端口、SNI 与证书绑定，至少修改一项：

```sh
proxyhub proxy hy2 list
proxyhub proxy hy2 create --server-name hy2.example.test --certificate-id <CERTIFICATE_ID>
proxyhub apply <CREATE_PLAN_ID>

proxyhub proxy hy2 update --name "备用 HY2" --port 4443 \
  --server-name other.example.test --certificate-id <CERTIFICATE_ID>
proxyhub apply <UPDATE_PLAN_ID>

proxyhub proxy hy2 disable
proxyhub apply <DISABLE_PLAN_ID>
proxyhub proxy hy2 enable
proxyhub apply <ENABLE_PLAN_ID>
```

应用中的本机健康检查使用锁定 Mihomo 经认证 QUIC 访问受控本机目标，并分别验证可信链/名称和实际证书指纹；这不能代替公网、应用 UDP 或指定 GUI 的真实请求验收。此前阶段 2d 已另行通过真实面板 HY2 生命周期、CLI 可信证书与 SNI 切换、REALITY/HY2 公网 TCP 请求及经 SOCKS 的 UDP 回显；同一旧 REALITY CONNECT 长连接跨升级、失败、启停、证书切换和恢复保持可用。自签名证书实际加载后的信任失败回滚、停用 HY2 的证书依赖保护及真实进程中断恢复均通过，390/1440 宽度无溢出、console 无错误。两 REALITY 与 Nginx PID 保持部署前基线；publisher 为代码升级重启一次，此后只 reload 且 PID 不变，manager/panel 也为升级重启。该历史结果不替代本轮共享组验证。公网 IPv6 与指定 GUI 客户端尚未验收，详细证据见[阶段 2d 验收](phase2d-hy2-acceptance.md)。其余选定协议的产品状态见[总方案协议矩阵](superpowers/specs/2026-10-14-proxyhub-design.md#6-proxy-与-rules)。

## 独立协议节点

“代理服务”的“独立协议节点”提供创建、编辑、启用、停用和删除，最多保存 32 个资源。每个节点有稳定编号和独立 IPv4 公网监听；TCP 节点不复用网站 TCP 443 或 REALITY 内部端口，SS 使用同端口 TCP/UDP，TUIC 使用 UDP。SOCKS5 的 UDP 通过 association 协商转发，实际 UDP 端口由服务协商；配置中的同号 UDP 端口仍保留用于冲突检查。已停用节点仍保留名称、端口、凭据和依赖；删除前必须先停用。操作不会自动修改防火墙。

| 协议 | 可创建的传输与选项 | 当前边界 |
| --- | --- | --- |
| VLESS | TCP、WS、H2、HTTPUpgrade；证书 TLS 或现有 REALITY 目标；TCP 可选 Vision | REALITY 只提供 TCP/H2；Vision 必须 TCP+TLS；锁定 Mihomo 的 H2 仅承诺 TCP；新建 gRPC 不提供 |
| Shadowsocks | 三种 AEAD 和三种 2022 方法，TCP/UDP 同端口 | 不提供插件；更新沿用密钥，方法与密钥长度不兼容会拒绝 |
| Trojan | 可信证书 TLS，TCP、WS、HTTPUpgrade | 锁定 Mihomo 不支持 Trojan H2，明确拒绝；新建 gRPC 不提供 |
| TUIC | v5，可信证书 TLS，bbr/cubic/new_reno，native/quic UDP 转发 | 不转换 v4 token，不关闭证书验证 |
| AnyTLS | 可信证书 TLS TCP | 不提供 REALITY 或 HTTP 传输 |
| VMess | AEAD TCP、HTTP/1 头、WS、H2、HTTPUpgrade | 固定 alterId=0；HTTP/1 头必须 TLS 关闭且 security=none；锁定 Mihomo 的 H2 仅承诺 TCP；不提供 QUIC/gRPC |
| SOCKS5 | 用户名/密码认证，TCP 和可选 UDP | 本身不加密，不提供 SOCKS4/TLS 包装；面板只创建带认证节点 |

锁定 Mihomo 的 H2 UDP 未通过验收；YAML 不声明 UDP 支持，仅承诺 H2 TCP。面板在选择 H2、节点详情和变更预览中显示该边界。配置包含 `udp: false` 节点时，在默认 `rule` 模式的代理匹配规则之后追加 `MATCH,REJECT`，防止不支持 UDP 的代理被跳过后落入默认 DIRECT；用户显式选择 `global` 或 `direct` 模式不属于该规则的范围。同一服务端经锁定 sing-box 客户端已通过 H2 TCP/UDP 验证，不能据此推断 Mihomo 的 UDP 也通过验收。服务端保持原 H2 配置，不会自动替换为 TCP 传输。

TLS 表单选择受管证书并填写其覆盖的 SNI，缺失或无效证书需先在“网站与证书”添加、导入或申请。VLESS REALITY 表单改为选择现有握手目标，SNI 跟随目标；引用的目标及本机站点、域名、证书依赖继续受到保护。节点停用不会解除依赖。共享布局下，任一启用成员使用的证书材料变化会切换整个共享组，HY2 与全部启用额外节点的现有连接均需重连，原 REALITY 进程保留；旧布局仍只调整相应独立进程。材料未变化不触发共享组重启。

创建自动生成独立凭据，名称、端口、传输选项更新和启停均保留凭据。普通资源和变更预览仅返回脱敏字段；“显示订阅 YAML”和客户端链接包含凭据，应按秘密材料保管。启用节点追加到原 REALITY、HY2 后方，已有默认选择与排序保留；停用节点从订阅移除。未加密的 VLESS/VMess 传输和 SOCKS5 会在页面及预览中明确提示，不能据服务运行状态推断公网连接已经验证。

CLI 使用相同计划。`--options` 接收 JSON 对象，或以 `--options-file` 读取不超过 16 KiB 的本机 UTF-8 JSON 文件；只向操作层提交解析后的字段，不提交文件路径。未知、重复或协议冲突字段会拒绝。创建需要协议、名称、端口和选项，更新仅修改明确给出的名称、端口或完整 options 对象；协议不能修改。

```sh
proxyhub proxy node list
proxyhub proxy node create --protocol vless --name "独立 VLESS WS" --port 5443 \
  --options '{"transport":"ws","tls":true,"sni":"site.example.test","certificate_id":"<CERTIFICATE_ID>","host":"site.example.test","path":"/proxy"}'
proxyhub apply <CREATE_PLAN_ID>

proxyhub proxy node update --node-id <NODE_ID> --name "节点新名称"
proxyhub apply <UPDATE_PLAN_ID>
proxyhub proxy node disable --node-id <NODE_ID>
proxyhub apply <DISABLE_PLAN_ID>
proxyhub proxy node enable --node-id <NODE_ID>
proxyhub apply <ENABLE_PLAN_ID>
# 删除前先停用并应用，再单独准备删除计划。
proxyhub proxy node delete --node-id <NODE_ID>
proxyhub apply <DELETE_PLAN_ID>
```

## 网站内容上传、预览与恢复

在“网站与证书”先选择当前管理的网站，再选择 ZIP，点击“验证并预览上传”。变更抽屉显示文件数、体积与服务影响，点击“加载页面预览”读取隔离预览；确认应用后才切换该网站公网内容。取消预览保留当前部署。内容版本列表仅展示所选网站的当前版本、文件数、体积和创建时间，可选择已成功应用的上传版本或默认模板，再预览并确认恢复。未应用、失败或属于其他网站的候选不能恢复到当前网站；阶段 2a 的既有历史继续归属默认网站。

ZIP 压缩体积不得超过 **180 KiB**，实际解压总量不得超过 **2 MiB**，文件和目录合计最多 **200 项**。根目录必须包含非空 UTF-8 `index.html`，该文件不超过 256 KiB。支持的静态扩展名为 `.html`、`.htm`、`.css`、`.js`、`.json`、`.txt`、`.svg`、`.png`、`.jpg`、`.jpeg`、`.gif`、`.webp`、`.ico`、`.woff`、`.woff2`、`.ttf`、`.otf`；不执行上传内容中的服务端代码。路径穿越、隐藏路径、符号链接、特殊文件、重复路径和大小写冲突会被拒绝。

上传和恢复沿用当前域名、证书、代理节点与订阅设置。恢复旧内容不会恢复当时的其他配置；后续订阅、证书和节点变更也会保留各站点已上传的内容。内容变更使用 Web 激活和失败恢复流程，重载 Nginx 与订阅发布器，保留 REALITY；共享运行配置未变化时保留 HY2/额外节点组进程，证书材料变化时按上文说明整个组的重连影响。网站标题输入仅修改所选网站的默认模板；使用上传内容时，应在本地修改页面后重新打包上传。

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

阶段 2b、2c、2d 与其余选定协议均已按明确支持范围验收。MRS 规则流水线、外部 URI 导入和自动计量仍属后续范围。

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
