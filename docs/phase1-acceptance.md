# 阶段 1：最小闭环验收

验收日期：2026-10-14。环境为用户授权的专用 Debian 12 amd64 VPS；客户端测试运行于 Windows。公开记录不保存主机身份、完整订阅链接、节点凭据或 SSH 私钥。

## 已通过

| 场景 | 实测结果 |
| --- | --- |
| SSH 公钥追加 | 读取实际 AuthorizedKeysFile 与账号上下文，备份并原子追加；另一个仅用密钥的连接成功；重复执行返回 noop。原端口、密码登录与 root 登录策略保留。测试使用本次生成的专用公钥，不是用户已有的人用密钥。 |
| 公钥认证原来关闭 | 独立安装仅启用 PubkeyAuthentication 的 drop-in；`sshd -t` 与生效配置核对通过后 reload，再进行上述连接验证。 |
| 网站与 REALITY 共用 TCP 443 | 公网 HTTPS 正常校验证书并读取网站；网站 SNI 先进入 REALITY，普通 HTTPS 转往 loopback 网站。订阅 SNI 进入独立 TLS 入口。 |
| 订阅导入与真实代理 | 公网 GET/HEAD 正文与元数据一致，错误令牌返回 404；下载的 YAML 经 Mihomo 1.19.32 校验并运行，真实 HTTPS 请求出口与测试 VPS 一致。未设置系统代理，临时客户端进程已退出。 |
| 手工元数据与覆盖层 | 单独发布手工测试流量、到期字段、名称、重置日与客户端覆盖；公网响应头与下载配置检查通过。随后恢复未知流量 `{}`，不把测试数值作为真实计量。 |
| 证书更新 | HTTP-01 首次签发成功；受控重新签发后调用产品续期入口，新证书指纹不同，网站和订阅 TLS 连接都实际返回新证书。再运行返回 unchanged。 |
| 自动续期 | 独立 `proxyhub-renew.timer` 已启动；仅接管有受管 ACME 来源标记的证书。外部导入证书跳过；配置并发变更拒绝覆盖。 |
| 激活失败恢复 | 注入候选健康检查失败，恢复上一版本的链接、发布内容与服务，并等待旧版本健康后结束事务。 |
| 进程中断恢复 | 子进程在写入 journal 并切换 current 后退出；recover 恢复旧版本，第二次 recover 返回 nothing_to_recover。 |
| 隔离与命令 | 仅安装 ProxyHub 目录、专属账号、`proxyhub` 和 `proxyhub-*` 服务。内部 2443/8443/8444/18890 均只监听 loopback；未覆盖既有项目、全局 sing-box 或原管理命令。 |

## 版本和自动化检查

- 服务端：sing-box 1.14.3、Nginx 1.30.5、Node.js 24.21.0、Python 3.11.2、Certbot 2.1.0、OpenSSH 9.2p1；发行包及来源摘要见阶段 0 证据。
- Windows：166 项测试通过，124 个 unittest 子用例通过；41 项依赖 POSIX 的测试跳过。后续证书跨平台回归增加一个子用例，证书相关 27 项与 125 个子用例通过。TypeScript 构建与真实 Windows sing-box/Mihomo 配置校验通过。
- VPS：176 项 Python 测试通过，包含全部 79 项 SSH 测试；结果见 `evidence/linux-acceptance.json`。转换器集成在 Windows 运行，Linux 上另以真实 Node 生成部署配置并经锁定核心校验。
- 独立审阅覆盖组件需求与集成质量。修复了证书版本覆盖、续期并发丢失更新、外部证书接管、回滚就绪等待和客户端保留名称冲突。
- 运行时首次实际启动暴露 Netlink 地址族限制，已在代理专属 unit 中允许 AF_NETLINK。失败部署完成回滚后再验收成功。
- Linux 复核发现 OpenSSL 3.0 的 hostname 不匹配也可能返回退出码 0；校验现要求明确的匹配结果，两个平台的回归通过。

机器内存约 4GB，三项运行服务的一次空闲 MemoryCurrent 合计约 34MiB；这不是 1 核 1GB 压力验收，也不含未来面板与流量采集开销。

## 复核入口与未覆盖项

`scripts/accept_live.py` 使用私有配置文件验证公网网站、订阅、覆盖层和实际代理出口；需要 requests、PyYAML 与锁定 Mihomo。`scripts/accept_recovery.py` 会重启本项目服务并注入故障，只应在专用测试环境显式运行。

Clash Verge Rev 2.5.8 图形界面导入、名称/额度显示仍未实际操作，不能用内核验收替代。arm64 和 Ubuntu 安装、完整 SSH 加固/防失联流程、网站内容编辑与依赖管理、HY2、规则流水线、多订阅、自动计量及重置、面板和 1GB 压力测试按后续阶段实施。
