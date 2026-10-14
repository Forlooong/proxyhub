# ProxyHub 知识摘要与核验记录

核验日期：2026-10-14。保存可公开的原则、设计依据和待验证能力，不保存私人部署地址、完整订阅 URL、令牌、私钥或原始运行日志。安装与运行不依赖个人知识库在线访问。

## 1. 知识库读取结果

已读取用户指定的《代理服务知识库》，页面修订时间为 2026-10-14。这里仅保留脱敏后的通用原则，不复制私人 Page 内容或访问权限。页面中标记“未读取”的教程只作为线索，未据此扩展功能承诺。

| 知识原则 | 模块落点 | 验收对应 |
| --- | --- | --- |
| 保留当前 SSH 会话、第二连接验证密钥后再加固；检查生效配置 | host 公钥追加、Match 上下文、账号/指纹验证、独立恢复 | HOST-1 / HOST-2 |
| 先单节点与真实请求，再分流、TUN、备用协议和链式代理 | 阶段 1 最小 REALITY 路径，后续 rules/sub 扩展 | SITE-1 / RULE-1 |
| 协议、服务端核心、客户端 GUI 和核心分开记录 | 部署 manifest 和客户端兼容表 | SUB-1 |
| REALITY 目标、节点连接地址、客户端 SNI 分开 | RealityTarget、Nginx 路由、内部网站监听 | SITE-1 / SITE-2 / SITE-3 |
| 配置只有一个管理入口，不能靠手改输出保留用户设置 | 唯一生成器和持久 ClientOverrides | SUB-2 |
| TCP/UDP、IPv4/IPv6、DNS/业务流量分别验收 | 端口计划、节点解析启动路径和实际连接测试 | SITE-1 / RULE-1 |
| 备份版本、凭据、证书和状态；副本检查后升级 | 备份恢复、journal、版本清单 | CORE-1 / OPS-1 |
| 流量重置明确周期、时区、对象、历史；本地归零不等于供应商归零 | TrafficAccount、BillingCycle、幂等事件 | FLOW-1 / FLOW-2 |
| 配置语法通过不等于公网可达，更不等于真实代理可用 | 分层健康状态，真实客户端门槛 | SITE-1 / SUB-1 |

知识库的 Xray 字段、Hysteria 独立服务端命令及第三方脚本用法不直接复制。ProxyHub 服务端使用 sing-box，客户端输出 Mihomo；例如 Xray 的 target/serverNames、公钥参数、mihomo 的 dialer-proxy 与 sing-box 的 handshake/server_name/detour 应逐项转换并验证。

## 2. 本地代码依据与候选版本

| 证据 | 本次核对结果 | 还不能据此声称的事项 |
| --- | --- | --- |
| `sing-box/docs/subscription.md` 第 60–66 行附近 | 已有手工 upload/download/total/expire；reset_day/reset_at/note 仅用于管理；刷新间隔 12 小时 | 已有实时统计、自动重置或所有客户端一致显示 |
| 同文档第 89 行附近及发布代码 | 已有低权限 loopback 只读 bundle，正文和响应头同版，加载失败保留旧版 | 已支持 ProxyHub 多订阅、共享账户和扩大后的容量目标 |
| `sing-box/docs/protocols.md` | 项目层支持/限制与核心上游能力分别列出 | 任意上游新协议参数均可无损导入 |
| `sing-box/versions.json` | 候选 sing-box 1.14.3、Nginx 1.30.5、Mihomo 1.19.32、Clash Verge Rev 2.5.8 | 仅有本地版本文件不能证明资产/能力可用；进一步核验结果见下表 |

本地 sing-box HEAD 为 `f5b33927bf61a91a772b9a9e5f2b06e54f6a7fcd`，有既存未提交修改；本次阅读的工作树文档不能归因于该提交的原始内容。规则仓库 HEAD 为 `e8cbc99509e561435eba521d7b02616f2be722e1`。复用时应从可追溯的选定提交/补丁导入，不能把整个脏工作树当成已审计上游。

上述文件属于本工作区原项目，未在本次修订中改动。新项目建立后应把经过选择的公共证据与测试结果固定到新仓库，避免文档永久依赖相邻仓库。

## 3. 官方文档核验

以下是文档或源码层依据，核验日期均为 2026-10-14；在线文档随版本更新，不等同于锁定发行包的运行结论。

| 来源 | 支持的事实 | 项目设计与待验证项 |
| --- | --- | --- |
| [Nginx ssl_preread](https://nginx.org/en/docs/stream/ngx_stream_ssl_preread_module.html) | stream 可读取 ClientHello 的 SNI 等信息而不终止 TLS；模块需要构建支持 | 相同 SNI 不足以区分浏览器和 REALITY；安装检查模块，真实测试分流路径 |
| [sing-box TLS](https://sing-box.sagernet.org/configuration/shared/tls/) | REALITY 使用其自身的 handshake 等字段 | 已进一步核对 1.14.3 源码；本机目标与普通 HTTPS 转发仍需实测；不照搬 Xray JSON |
| [Xray REALITY](https://xtls.github.io/config/transports/reality.html) | 用于理解 REALITY 目标与非认证连接行为 | 仅为机制参考，不证明 sing-box 目标版本具有完全相同行为 |
| [ACME challenge types](https://letsencrypt.org/docs/challenge-types/) | HTTP-01 使用 TCP 80；通配符需要其他支持方式；DNS-01 可自动化 | HTTP-01 首版，DNS-01 扩展；验证 A/AAAA、端口、失败重试与实际证书加载 |
| [OpenSSH sshd_config](https://man.openbsd.org/sshd_config) | AuthorizedKeysFile、Match、AuthorizedKeysCommand 等影响有效认证 | 按账号/连接上下文解析；不默认向 ~/.ssh/authorized_keys 追加 |
| [OpenSSH sshd](https://man.openbsd.org/sshd) | authorized_keys 授权项可带限制选项，可能限制转发 | 相同 key 不同 options 不能无条件追加；登录与面板隧道分别验证；实际发行版行为须验证 |
| [sing-box V2Ray API](https://sing-box.sagernet.org/configuration/experimental/v2ray-api/) | 统计 API 不是默认包含项；支持按配置的 inbounds/outbounds/users 统计 | 检查构建能力与每协议粒度；生命周期、方向、异常重启误差需运行测试 |
| [Clash Verge Rev v2.5.8 解析源码](https://github.com/clash-verge-rev/clash-verge-rev/blob/v2.5.8/src-tauri/src/config/prfitem.rs) | 读取流量/到期、按小时解释刷新间隔、filename* 名称及 HTTP/HTTPS 主页；未以 Profile-Title 设置名称 | 已替换用户引用的可变 dev 分支作为版本依据，仍需实际 GUI 导入验收 |

### 锁定版本源码与发布元数据

| 对象 | 已核验 | 待运行验证 |
| --- | --- | --- |
| [sing-box v1.14.3 release](https://github.com/SagerNet/sing-box/releases/tag/v1.14.3) | 发布存在；GitHub 资产元数据中的 Linux amd64/arm64 tar.gz digest 与本地 versions.json 一致 | 尚未下载本轮资产或执行二进制，不能称独立文件哈希验收 |
| [REALITY 服务端](https://github.com/SagerNet/sing-box/blob/v1.14.3/common/tls/reality_server.go) | 单一 ServerName；拒绝 certificate_provider/acme/certificate/key | 双入口保留旧 SNI、目标切换与客户端真实请求 |
| [go.mod](https://github.com/SagerNet/sing-box/blob/v1.14.3/go.mod) / [utls v1.8.7](https://github.com/MetaCubeX/utls/blob/v1.8.7/reality.go) | 锁定该依赖；可见连接目标、TLS 1.3/SNI/认证与非认证连接转发路径 | 目标密钥交换兼容性、普通浏览器与 REALITY 共存 |
| [sing-box 证书加载](https://github.com/SagerNet/sing-box/blob/v1.14.3/common/tls/std_server.go) | 新密钥对校验后更新内存配置 | 原子切换目录/符号链接是否被 watcher 捕捉，所有消费者实际证书指纹 |
| [Nginx release-1.30.5](https://github.com/nginx/nginx/blob/release-1.30.5/src/stream/ngx_stream_ssl_preread_module.c) | 对应标签和 preread 实现已读取 | 实际包 stream/ssl_preread 构建及加载 |
| [默认构建标签](https://github.com/SagerNet/sing-box/blob/v1.14.3/release/DEFAULT_BUILD_TAGS) / [stub](https://github.com/SagerNet/sing-box/blob/v1.14.3/include/v2rayapi_stub.go) | 默认标签无 with_v2ray_api；缺失时有明确构建错误 | ProxyHub 计量构建须由同一上游提交加标签构建并实际探测 |
| [统计实现](https://github.com/SagerNet/sing-box/blob/v1.14.3/experimental/v2rayapi/stats.go) | 内存计数，可非破坏读取或清零；多维度可能重复覆盖同一流量 | reset=false 采样、代次识别、协议/用户粒度、重启与跨周期缺口 |
| [CVR v2.5.8 UI](https://github.com/clash-verge-rev/clash-verge-rev/blob/v2.5.8/src/components/profile/profile-item.tsx) / [流量显示](https://github.com/clash-verge-rev/clash-verge-rev/blob/v2.5.8/src/utils/parse-traffic.ts) | 标签提交为 4337bd85758d54d7b9044b2da3c9ac1176eae1f3；用 upload+download、total 与 Unix 秒 expire | 实际客户端显示、本地覆盖选项、指定 Mihomo 核心兼容 |

Mihomo 1.19.32、本机系统包、ACME 客户端与最终自建计量资产仍须单独取得发行和运行证据。源码存在不证明系统已安装该能力。

## 4. 需要固定到发行清单的证据

实施阶段 0 为每项记录：仓库、完整 commit/tag、上游 URL、许可证、下载资产、SHA256、构建标签、架构、核验日期、核验方式及通过/失败/未测试。

- sing-box：版本输出、REALITY/QUIC/V2Ray API 能力、配置校验；实际样本验证 TCP/UDP、用户/入站计数方向和持久采样。
- Nginx：版本和编译参数、stream/ssl_preread 动态模块装载、SNI 路由与 HTTP-01；对所用发行版的包修订号单列。
- 客户端：GUI 版本、实际 Mihomo 核心版本和配置；导入/更新/显示/真实请求均记录，不能仅记一个客户端产品名称。
- ACME 客户端与 OpenSSH：实际发行包版本、验证/续期任务和有效认证配置，不按在线最新版手册推断发行版行为。
- 第三方来源：六个仓库逐项固定许可证与选定文件，不复制私人部署数据；前端设计参考不授予其代码/图片复用权。

本机尚未运行新系统、真实 VPS 共存、ACME 签发、SSH 登录或指定客户端导入测试。设计验收表是待实施清单，不是已有测试报告。

## 5. 本次决策记录

1. 保留低权限静态 bundle 发布进程。它保持正文与响应头一致，管理器停止仍能服务，避免用频繁 Nginx reload 更新流量元数据。
2. 增加共享 TrafficAccount，订阅最多选择一个账户产生标准流量头。其他来源分开展示，避免同一 VPS/节点因多协议、多订阅重复相加。
3. 本机计量持久化增量与代次，明确无法恢复的未采样区间；周期重置新建账期，不删除终身累计。
4. 网站、证书、目标和域名分别建模；增加删除依赖检查与双路径域名退役。证书签发和消费者成功加载是不同状态。
5. SSH 公钥追加识别限制选项与实际有效文件；第二个密钥会话的证据是关闭旧认证方式的前置条件。
6. 完整外部 YAML 导入延后，首版如实报告只导节点及忽略字段；用户自定义配置保存在唯一覆盖入口，刷新不丢失。
7. 设计文档与证据可以公开进入新项目；个人知识库在线访问不是安装、运行或构建依赖。
8. 保留标准官方内核，并新增经 CI 验证的计量构建选择，不把默认发行包误报为支持 V2Ray API；切换必须经过可预览的内核变更操作。
