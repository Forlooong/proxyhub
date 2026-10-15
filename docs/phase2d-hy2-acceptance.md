# 阶段 2d：HY2 与协议能力矩阵验收

日期：2026-10-15（Asia/Shanghai）。版本：`0.6.0a1`。沿用专用 Debian 12 amd64 VPS、sing-box 1.14.3、Mihomo 1.19.32、Nginx 1.30.5 和 Node 24.21.0。**本机基础 HY2 增量通过；完整阶段 2 与首版发行仍未完成。** 其余协议的已实现、待实现及不支持状态见[总方案第 6 节](superpowers/specs/2026-10-14-proxyhub-design.md#6-proxy-与-rules)。

## 交付与边界

新增一个可选 HY2 资源，使用稳定节点/监听编号、独立随机密码、名称、IPv4 UDP 端口、SNI 与证书绑定。创建、更新、启用和停用均通过共享操作层显式预览/应用；更新和重新启用保留凭据，停用保留证书依赖。初始普通部署和其他激活模式不能绕过这些操作。默认监听 UDP 443，与原 TCP 443 共存；没有开启网站 HTTP/3。

独立低权限 HY2 服务只读取其受保护的 `hy2.json`。所选 PEM 材料内嵌于 `0640 root:proxyhub-proxy` 的不可变运行配置，原证书归档目录与私钥权限不放宽。模型资源和普通预览不返回密码或 PEM。受管材料路径变化时仅重启 HY2；实际健康检查分别验证普通信任链/名称与所选叶证书指纹，两个独立 QUIC 客户端都须完成认证代理请求。Mihomo 的叶证书 pin 会替代 PKI 验证，因此不能用 pin 单独证明证书可信。

字段与行为核对了锁定版本的 [sing-box HY2 选项](https://github.com/SagerNet/sing-box/blob/v1.14.3/option/hysteria2.go)、[TLS 材料加载](https://github.com/SagerNet/sing-box/blob/v1.14.3/common/tls/std_server.go)、[Mihomo HY2](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/adapter/outbound/hysteria2.go)及[证书 pin 实现](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/component/ca/fingerprint.go)。没有增加 obfs、带宽、端口跳跃、realm、ECH 或外部 URI 导入等未验收参数。

唯一转换器在原 REALITY 后追加启用的 HY2；Mihomo 手选组继续优先 REALITY，原始订阅输出两行 URI 的 Base64。HY2 停用后从两种订阅移除，服务器元数据和用户覆盖不变。CLI、面板及升级操作见[面板手册](panel.md)。

## 验收结果

| 检查 | 结果 |
| --- | --- |
| Linux 相关测试 | 483 个不同测试及 125 项 subtests 通过。首轮 481 passed、1 skipped；指定现有锁定 sing-box 后仅补跑跳过项，1 passed。嵌入证书修正后重跑受影响的 29 项激活测试；新增 Host authority 回归另补 1 passed，不将重跑项累计为新覆盖 |
| 本机专项 | 模型、转换器、发布校验共 112 项通过，包含锁定 Mihomo 单/双节点配置检查；CLI 与新增 HY2 管理检查 102 项通过。各组与 Linux 有交集，不相加推算覆盖率 |
| 前端与操作 | TypeScript、Vite 通过。真实 Edge 完成创建、更新、停用、启用及显式应用；390px/1440px 无横向溢出，最终功能会话 console 0 errors / 0 warnings |
| 公网真实客户端 | 个人电脑使用锁定 Mihomo，分别选择 REALITY 与 HY2 完成公网 HTTPS 代理请求，出口地址确为测试 VPS；两种协议还分别通过 SOCKS5 UDP 转发至 VPS loopback 回显服务 |
| 证书切换 | CLI 将 HY2 从原可信证书/SNI 切换到第二站已有可信证书/SNI；两个不同叶指纹的实际加载、认证 QUIC 及公网 TCP/UDP 请求均通过，没有强制重新签发已有证书 |
| 新证书失败恢复 | 临时绑定仅由 HY2 引用。生成 SAN、日期、密钥有效但不受信任的自签名材料，导入及静态检查通过，实际 HY2 信任检查失败；旧材料与服务、发布恢复，revision 不提交、计划 failed、journal 清理。未用网站失败代替 HY2 消费者失败 |
| 依赖与启停 | 停用后服务 inactive，订阅移除 HY2；重新启用恢复服务和订阅。停用的 HY2 独占证书仍不可删除，临时绑定最终已解除引用并删除 |
| 激活失败回退 | 候选已完成真实认证 QUIC 与证书校验后注入失败，明确断言已到达注入点；旧状态恢复且未提交，新 UDP 监听释放。不能将较早的意外失败算成故障注入通过 |
| 真进程中断 | 独立进程写入 activating journal、切换 current 并发布磁盘文件后执行 `os._exit(86)`；恢复旧资源/服务/发布成功，再次恢复无待恢复项，revision 不提交 |
| 无效输入与占用 | SAN 不匹配拒绝；准备计划后临时占用目标 UDP 端口，应用在切换前拒绝。未知参数、凭据篡改、其他模式绕过、不可变候选及恢复失败保留 journal 另有针对性回归 |
| 订阅与元数据 | 最终 YAML/raw GET/HEAD 元数据一致，未知令牌拒绝；双协议顺序和 REALITY 默认优先、HY2 可信 TLS、客户端覆盖保留均通过，当前完整订阅导入和真实请求通过 |
| 原服务与旧连接 | 两个 REALITY 实例及 Nginx 主 PID 从升级前基线始终不变。同一原 SNI CONNECT 长连接跨升级、失败回退、HY2 启停、证书切换和中断恢复仍可收发；服务器、网站、REALITY、订阅元数据、覆盖配置摘要最终与原基线一致 |
| 升级发布器 | 旧常驻 Python 发布器不会因 reload 加载新协议校验代码；在旧有效 bundle 当前时重启一次受管 publisher。此后各操作只 reload，PID 保持。管理器/面板为代码升级重启；不宣称全部服务 PID 全程不变 |
| 部署一致性 | 最终 283 个打包文件 SHA-256、root 属主与不可组/其他用户写入权限逐项一致；原五项常驻服务与独立 HY2 均 active，保留的另一 REALITY 实例也 active |
| 独立审查 | 关闭首次普通部署绕过 HY2、低权限读取归档证书及证书变更影响说明问题；最终运行边界复核无剩余阻塞 |

验收修复了健康请求 URL 与 Host 不一致的问题：Mihomo 会按 Host 选择目标，旧请求可能改拨本机 80；真实请求回归先失败、修复后通过，最终目标固定为受控发布端口。另核对了 Mihomo mixed 监听先于 `OnRunning()` 就可接受连接的启动窗口；连接端口可用不代表已能代理，保留原有 3 次有界健康等待，不把单次失败直接认定为启动瞬态。

| 网络项 | 本次结果 |
| --- | --- |
| IPv4 REALITY / TCP 传输 | HTTPS 代理与应用 UDP 转发通过，旧/新 SNI 继续保留 |
| IPv4 HY2 / UDP QUIC 传输 | HTTPS 代理与应用 UDP 转发通过，与 TCP 443 网站/REALITY 共存 |
| 公网 IPv6 / TCP、UDP | VPS 没有公网 IPv6，未启用、未验证；本次 HY2 实现明确为 IPv4 监听 |

最终保留原两个 REALITY 入口、两个网站、现有证书和启用的 HY2；没有修改 SSH、防火墙或原命令。临时客户端、回显服务、源码测试副本、诊断日志及测试 PEM 按本次范围清理，保留一份升级前应用备份；正常计划/失败记录保留为操作历史。私钥不离开服务器，公开记录不保存真实部署身份、域名、凭据或订阅链接。

## 后续

继续按总方案逐项收口阶段 2 的其余协议门槛，再进入阶段 3。IPv6、指定 Clash Verge Rev GUI、1 核 1GB 指标、通用升级/备份恢复等仍须分别验收；本次不将基础 HY2、协议矩阵或现有核心能力当成完整阶段 2/首版完成。
