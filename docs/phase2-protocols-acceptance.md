# 阶段 2：其余选定协议收口验收

日期：2026-10-16（Asia/Shanghai）。版本：`0.7.0a1`。沿用专用 Debian 12 amd64 VPS，sing-box 1.14.3、Mihomo 1.19.32、Nginx 1.30.5、Node 24.21.0 均未升级。**总方案第 6 节明确支持的本机协议范围通过，阶段 2 据此收口；完整首版仍未完成。** Trojan H2 明确拒绝，H2 UDP 未纳入锁定 Mihomo 支持范围，外部 URI 导入与独立 Direct 转发没有实现。公网 IPv6、指定 GUI 及发行资源门槛仍未验证。

## 交付

新增最多 32 个受管独立节点，覆盖 VLESS、VMess、Trojan、Shadowsocks、TUIC v5、AnyTLS、SOCKS5。稳定 ID、独立 IPv4 监听和随机凭据均由模型校验；名称、传输、端口更新及启停不换凭据。删除须先停用，停用仍保留端口、证书与 REALITY 目标依赖。CLI `proxyhub proxy node` 与面板共用类型化计划，普通资源、预览和任务结果不返回凭据。

每个节点使用 `proxyhub-node@<id>.service`，只读取 `nodes/<id>.json`。证书 PEM 内嵌于 `0640 root:proxyhub-proxy` 不可变运行配置，证书归档 `0700` 与私钥 `0600` 不放宽。激活只调整变化实例与订阅发布，恢复先校验候选 manifest 与类型化差异；恢复失败保留 journal。证书指纹取自本次运行配置中的叶证书，普通 PKI/名称验证与叶指纹验证分别执行。

唯一转换器追加启用节点，保留原 REALITY/HY2 排序、默认选择、用户覆盖和元数据。Mihomo YAML 与通用 URI 同源，供运行健康检查使用的 JSON proxies 也来自同一转换器，不新增第二个客户端渲染器。

## 协议矩阵实测

每组均用锁定 Mihomo 连接 VPS 公网监听，完成认证后的 TCP 回显与公网 HTTPS 出口确认；UDP 使用受控应用回显。测试客户端末尾设置拒绝规则，防止不支持 UDP 的代理被跳过后默认 DIRECT。

| 协议 | 组合数 | 实测范围 |
| --- | ---: | --- |
| VLESS | 11 | 证书 TLS TCP/WS/H2/HTTPUpgrade，明文 TCP/WS/HTTPUpgrade，REALITY TCP/H2，证书与 REALITY TCP Vision。11 组 TCP/HTTPS 通过，9 组 UDP 通过；2 个 H2 验证客户端 UDP 拒绝边界 |
| VMess | 14 | TLS/明文 TCP 的四种 AEAD security、TLS/明文 WS/HTTPUpgrade、TLS H2、明文 HTTP1 none。14 组 TCP/HTTPS、13 组 UDP 通过；H2 验证客户端 UDP 拒绝边界 |
| Trojan | 3 | 证书 TLS TCP/WS/HTTPUpgrade 的 TCP/UDP/HTTPS 全部通过；H2 不提供 |
| Shadowsocks | 6 | 三种 AEAD 与三种 2022 方法的 TCP/UDP/HTTPS 全部通过，密钥长度匹配方法 |
| TUIC v5 | 6 | bbr/cubic/new_reno × native/quic 的 TCP/UDP/HTTPS 全部通过，服务端与客户端显式 ALPN h3 |
| AnyTLS | 1 | 可信证书 TLS TCP、应用 UDP 与 HTTPS 通过 |
| SOCKS5 | 4 | 认证/无认证 × UDP 开/关，四组 TCP/HTTPS、两组 UDP 转发、两组关闭拒绝通过；结束恢复认证与 UDP 开启 |

合计 **45 组 TCP/HTTPS、40 组 UDP 转发、5 组关闭边界通过**。另外，将客户端强制设为 UDP 开启后，关闭 UDP 的 SOCKS5 服务仍拒绝回显，证明服务端开关生效。SOCKS5 UDP association 的转发端口由服务协商，不声称固定同号监听；模型仍预留同号 UDP 端口用于冲突保护。

个人电脑另行下载真实公网 YAML，使用锁定 Windows Mihomo 逐个选择 9 个当前条目，再追加原 UUID/旧 SNI 客户端，共 **10 个入口的公网 TCP、UDP、HTTPS 全部通过**。此项验证了外部网络可达性；没有用 VPS 上访问自身公网地址替代个人电脑验收。完整下载配置的 Mihomo 导入、默认 REALITY 请求、用户覆盖、GET/HEAD 与未知令牌拒绝也通过。

## 发现与明确边界

- **Trojan H2**：锁定 Mihomo 的 Trojan transport 分支没有 H2，填写 H2 会落到默认 TCP。本产品在模型、转换器和面板明确拒绝，未将来源项目的 H2 声明记为已实现。依据：[锁定 Trojan 实现](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/adapter/outbound/trojan.go)。
- **H2 UDP**：VLESS TLS H2 的 Mihomo UDP 连续失败，换 packetaddr 仍失败；同一服务端、同一 H2/XUDP 配置使用锁定 sing-box 客户端时 TCP/UDP 通过。H2 的 Mihomo YAML 因此声明 `udp:false`，仅承诺 TCP。锁定 [H2 连接实现](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/transport/vmess/h2.go)的首次读写初始化缺少显式同步是可能原因，未被证明为根因；没有修改或升级锁定核心。
- **UDP 默认 DIRECT**：`udp:false` 下曾观察到的本机回显成功来自规则跳过后的 DIRECT，不能算 H2 成功。使用不存在的 SOCKS5 代理进行对照，无拒绝尾规则时回显成功，加 `MATCH,REJECT` 后拒绝，已真实复现。生成器只在额外节点含 `udp:false` 时增加该尾规则，原 REALITY/HY2 输出规则保留；显式 global/direct 模式不使用此 rule 边界。依据：[规则匹配](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/tunnel/tunnel.go)、[选择组 UDP 能力](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/adapter/outboundgroup/selector.go)。
- **TUIC ALPN**：原服务虽启动，真实客户端报 `CRYPTO_ERROR 0x178: server did not select an ALPN protocol`；仅增加服务端 `tls.alpn:[h3]` 后 TCP/UDP 均通过。锁定 [TUIC 服务](https://github.com/SagerNet/sing-quic/blob/75c3ac4fa12b/tuic/service.go)不补默认 ALPN，而 [HY2 服务](https://github.com/SagerNet/sing-quic/blob/75c3ac4fa12b/hysteria2/service.go)会补 h3，已据此修正渲染。
- **立即重新启用**：真实面板暴露 TCP TIME_WAIT 被端口预检误认为占用。Linux 回归先证明普通 bind 被 TIME_WAIT 拒绝，随后验证与监听一致的 `SO_REUSEADDR` 策略可用，并仍拒绝真正 LISTEN 的套接字。修复后面板立即停启循环通过。
- **原始 URI**：HTTPUpgrade、Trojan WS Host、SOCKS UDP 开关及 H2 UDP 声明不能由锁定 Mihomo 原始 URI 导入器无损恢复。通用分享 URI 保留协议参数，完整交付基准为 Mihomo YAML；没有声称 raw 重新导入逐组合通过。详见[转换器说明](../packages/converter/README.md)与[锁定 URI 导入器](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/common/convert/converter.go)。

## 生命周期、证书与恢复

| 检查 | 结果 |
| --- | --- |
| 真实面板 | 认证 SOCKS5 创建、改名换端口、停用、立即启用循环、先停后删通过；390/1440 页面及窄屏确认抽屉无横向溢出，console 0 errors / 0 warnings |
| 可信证书切换 | AnyTLS 在两份已有可信证书/SNI 间切换并恢复；不同叶指纹、普通 PKI 与精确叶指纹认证均通过，凭据不变，未强制重签已有证书 |
| 不受信任证书 | 独立临时绑定使用 SAN、日期、私钥匹配的自签名证书；静态准备通过。精确叶 pin 的认证请求证明新证书已实际加载，随后普通 PKI 拒绝；旧节点、发布、凭据与 revision 恢复，计划 failed、journal 清理，临时绑定删除 |
| 认证后注入失败 | 候选已通过真实认证、PKI 与叶指纹后才注入失败；明确断言到达注入点，旧配置与发布恢复，新端口关闭，revision 不提交 |
| 真实进程中断 | 独立进程在 journal/link/publish、实例启动及候选认证通过后 `os._exit(86)`；恢复旧节点与发布成功，再次恢复无待恢复项，新端口关闭 |
| 准备后占用 | 计划准备后占用目标 TCP 端口，应用在切换前拒绝，current/revision 不变且无 pending |
| 停用独占依赖 | 临时可信绑定仅被一个已停用 AnyTLS 节点引用，删除准确拒绝；重新启用、恢复原绑定后再删除临时绑定，凭据保持 |
| 隔离与不可变性 | 单元测试覆盖其他模式/类型化越权、凭据篡改、其他节点保留、缺失/改动/额外候选、TCP/UDP 占用、恢复失败保留 pending 及恢复前防篡改 |
| 源材料原路径变化 | 最小回归覆盖以旧运行配置内嵌叶指纹恢复，及 HY2 对新旧 `hy2.json` 材料差异的检测；VPS 的实际证书切换使用版本化路径，未将单元测试记成原路径替换的 VPS 演练 |

六项 VPS 故障/依赖演练全部通过，整轮结束配置恢复，其他六个兄弟节点和原服务的 PID 在演练期间保持不变。

## 原服务与验证记录

原 `server/web/subscription/overrides/reality/hy2` 六份摘要与升级前基线一致。两 REALITY 实例、HY2 与 Nginx PID 保持；publisher 为加载新协议校验器重启一次，此后只 reload，PID 保持。管理器与面板随代码更新重启，不能笼统声称全部服务 PID 全程不变。

升级前建立的旧 SNI、新 SNI、HY2 三条认证 TCP 长连接跨升级、全部协议操作与故障演练持续回显，各完成 3,917 轮且无错误。最终部署包 284 个文件与 VPS 一致，14 项相关服务 active。两个网站公网 HTTPS 正文与不可变版本哈希相同；最终 YAML/raw 各 9 条，GET/HEAD 及跨格式元数据一致，原 REALITY 默认、HY2 次序与用户覆盖保留。安装器仍只管理 `proxyhub` 与自有单元，原命令保留。

Linux 相关回归合计 **589 个不同测试、125 项 subtests 通过**。首轮 579 passed、9 failed：8 个旧激活 fixture 被无节点时多余的服务字段校验阻断，已使空节点资源直接返回；另 1 项为隔离 runner 缺少 src 路径，补正确路径后，仅重跑这 9 项全部通过。新增 TIME_WAIT 回归先在旧代码失败，修复后连同两个既有占用用例共 3 项通过，重跑项不重复累计。

本机另完成模型、转换器/发布器、CLI/管理层最小专项，含 26 种服务端组合的锁定 sing-box check 和 41 种客户端变体的锁定 Mihomo 检查。TUIC ALPN、H2/UDP 拒绝策略变更后仅补跑受影响用例；TypeScript/Vite 构建通过，最终只读交叉审查无阻断问题。专项与 Linux 有交集，不相加推算覆盖率。

保留七种安全默认验收节点及原入口，其他方法/传输实验节点、面板临时节点和临时证书绑定已移除。一次性客户端、回显进程、浏览器会话、私有实验文件、测试运行时与旧前端构建已清理；只保留本次应用升级的 root-only 回退归档。临时导入若复用了既有证书归档，该归档保留。记录不包含真实服务器身份、域名、凭据或完整订阅。

下一阶段为订阅与规则；指定 GUI、外部导入、多订阅、计量、完整 Host/CLI 契约、平台与发行资源指标仍按[总方案](superpowers/specs/2026-10-14-proxyhub-design.md#10-实施阶段和交付门槛)承接。
