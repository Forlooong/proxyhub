# 阶段 0：公开上游来源、版本与能力核验

核验日期：2026-10-14。机器可读依据为 [sources.lock.json](../evidence/sources.lock.json)。

本记录确认固定提交的存在、许可证文本、源码能力和官方发行元数据。它不把源码存在等同于二进制能力，也不把配置校验等同于真实网络验收。每项 `verified` 只针对其 `verification_level` 或 `level`；未执行的下载哈希、二进制运行、SSH 登录、证书签发和客户端 GUI 导入仍标为 `not_verified`。后续实际执行应新增运行证据，不抹去这个初始审计范围。

## 1. 六个候选项目的固定来源

使用 GitHub REST 将默认分支解析到完整提交，再按该提交读取完整递归树和 LICENSE。下表中的分支仅是发现入口，后续审阅或选择代码必须使用完整提交。

| 项目 | 固定提交 | 许可证依据 | 取舍 |
| --- | --- | --- | --- |
| [233boy/sing-box](https://github.com/233boy/sing-box/commit/2d78583b5aecccb0da0148a816bc1495a284509f) | `2d78583b5aecccb0da0148a816bc1495a284509f` | GPL-3.0 | reference_only |
| [Forlooong/sing-box](https://github.com/Forlooong/sing-box/commit/c5f001681939c23a9c69f80ee86363a8f6adba39) | `c5f001681939c23a9c69f80ee86363a8f6adba39` | GPL-3.0 | selective_reuse_candidate |
| [Forlooong/clash-rules-mrs](https://github.com/Forlooong/clash-rules-mrs/commit/e8cbc99509e561435eba521d7b02616f2be722e1) | `e8cbc99509e561435eba521d7b02616f2be722e1` | GPL-3.0 | selective_reuse_candidate |
| [CRThu/clash-rules-mrs](https://github.com/CRThu/clash-rules-mrs/commit/c03eb704a9c237d65f114d1b2a6f7fe428a935c6) | `c03eb704a9c237d65f114d1b2a6f7fe428a935c6` | 未发现许可证 | reference_only_no_license |
| [yuju520/YujuToolBox](https://github.com/yuju520/YujuToolBox/commit/d04baec205772ecd14820c36b98108f0596f1100) | `d04baec205772ecd14820c36b98108f0596f1100` | GPL-3.0 | reference_only |
| [SubBoost/subboost](https://github.com/SubBoost/subboost/commit/a7177d7bb704998bfb9f51116441506729fc43c0) | `a7177d7bb704998bfb9f51116441506729fc43c0` | AGPL-3.0-only | selective_reuse_candidate |

- **233boy/sing-box**：参考协议和管理操作覆盖；不引入安装器、全局路径及旧 `sb` 入口。
- **Forlooong/sing-box**：选择性审阅纯配置生成、订阅发布、事务和证书同步；当前工作区的未提交修改不属于上述固定远端提交。旧入口、迁移和重复规则运行时代码不带入新项目。
- **Forlooong/clash-rules-mrs**：选择性审阅快照、DNS 约束、发布和审计。个人规则不进入公共默认集。规则数据各自的授权和归属另见该提交的 [NOTICE.md](https://github.com/Forlooong/clash-rules-mrs/blob/e8cbc99509e561435eba521d7b02616f2be722e1/NOTICE.md)，不能用项目代码的 GPL 文本代替所有输入数据授权。
- **CRThu/clash-rules-mrs**：完整递归树没有 LICENSE、COPYING 或 NOTICE，仓库元数据也未给出许可；只参考 MRS 分类需求，不复制脚本。未来如取得授权，必须另留证据再改变取舍。
- **YujuToolBox**：参考初始化和可选诊断清单；新写防失联、幂等操作。原脚本写入 `/usr/local/bin/yuju` 等行为不执行。
- **SubBoost**：后续可选择性复用 parser/normalizer 的纯逻辑，固定 package.json 明确为 AGPL-3.0-only。暂不复制代码；正式复用须登记文件、修改和归属。其 Next.js/数据库架构、默认规则注入、静默舍弃不支持节点的行为不继承。

此次审计没有向新项目复制这些项目的业务代码，也没有执行任何候选安装脚本。来源锁保存候选复用清单；每次实际导入仍需文件级记录和适用许可证。

## 2. 目标核心与客户端版本

| 组件 | 固定提交 | 固定版本许可证 |
| --- | --- | --- |
| [SagerNet/sing-box v1.14.3](https://github.com/SagerNet/sing-box/commit/7054cac5124465ffdb145870d7c01885566711b1) | `7054cac5124465ffdb145870d7c01885566711b1` | GPL-3.0-or-later with additional naming restriction |
| [nginx/nginx release-1.30.5](https://github.com/nginx/nginx/commit/4556c714d1cfa587f92c7c0365c09209d42d148b) | `4556c714d1cfa587f92c7c0365c09209d42d148b` | BSD-2-Clause |
| [MetaCubeX/mihomo v1.19.32](https://github.com/MetaCubeX/mihomo/commit/88dcbf7f1614a67c3b36b848ee3592dfa92ada36) | `88dcbf7f1614a67c3b36b848ee3592dfa92ada36` | GPL-3.0 |
| [clash-verge-rev/clash-verge-rev v2.5.8](https://github.com/clash-verge-rev/clash-verge-rev/commit/4337bd85758d54d7b9044b2da3c9ac1176eae1f3) | `4337bd85758d54d7b9044b2da3c9ac1176eae1f3` | GPL-3.0 |
| [MetaCubeX/utls v1.8.7](https://github.com/MetaCubeX/utls/commit/f7d52c22f3a8d2f510ad1470f75cb6c3fe26aa37) | `f7d52c22f3a8d2f510ad1470f75cb6c3fe26aa37` | BSD-3-Clause |

这里的版本是项目选定基线，不宣称是最新版本。Nginx 官方下载页也列出 1.30.5，固定源码 `src/core/nginx.h` 声明 1.30.5。

需要特别保留两项区别：

- Mihomo 当前仓库 API 的许可证分类为 MIT，而 **v1.19.32 固定提交的 LICENSE 是 GPLv3**；发行按固定文件处理，不按当前仓库标签猜测。
- sing-box 1.14.3 文件为 GPL-3.0-or-later，另含派生作品名称及关联限制。自建计量发行必须保留许可并审查命名；不能仅根据 GitHub 的 NOASSERTION 判断没有许可证。utls v1.8.7 由该版本 `go.mod` 固定，并独立锁定其提交。

## 3. 已确认的源码能力及实现影响

| 能力 | 源码核验结果 | 实现与验收约束 |
| --- | --- | --- |
| REALITY 的 SNI | [sing-box REALITY 服务端](https://github.com/SagerNet/sing-box/blob/7054cac5124465ffdb145870d7c01885566711b1/common/tls/reality_server.go) 将一个 `server_name` 放入单元素集合 | 域名迁移需保留独立旧、新入口；不能向本版本入口生成 Xray 风格多个 serverNames |
| REALITY 目标与证书 | 同一源码通过 handshake 连接目标，并拒绝 REALITY 自身直接设置证书、密钥、ACME、certificate_provider | 网站 TLS 证书属于 HTTPS 目标；握手走内部地址，避免回到公网 443 形成循环 |
| 非认证 HTTPS | [utls REALITY 实现](https://github.com/MetaCubeX/utls/blob/f7d52c22f3a8d2f510ad1470f75cb6c3fe26aa37/reality.go) 存在 TLS 1.3、SNI、认证和目标转发路径 | 普通浏览器与 REALITY 共用网站 SNI 先进入 REALITY；目标曲线与握手特征要用真实请求验证 |
| 默认统计能力 | [默认构建标签](https://github.com/SagerNet/sing-box/blob/7054cac5124465ffdb145870d7c01885566711b1/release/DEFAULT_BUILD_TAGS) 有 with_utls/with_quic，无 with_v2ray_api；[缺失能力 stub](https://github.com/SagerNet/sing-box/blob/7054cac5124465ffdb145870d7c01885566711b1/include/v2rayapi_stub.go) 返回明确错误 | 最小闭环采用官方核心和手工元数据；自动采集须等经验证的可选计量构建，不能假定官方默认包支持 |
| Nginx SNI 分流 | [stream preread 模块](https://github.com/nginx/nginx/blob/4556c714d1cfa587f92c7c0365c09209d42d148b/src/stream/ngx_stream_ssl_preread_module.c) 读取 SNI | 同一 SNI 不区分浏览器/REALITY；还须验证实际包的 stream/ssl_preread 编译和装载 |
| Mihomo REALITY 客户端 | [VLESS outbound](https://github.com/MetaCubeX/mihomo/blob/88dcbf7f1614a67c3b36b848ee3592dfa92ada36/adapter/outbound/vless.go) 包含 reality-opts、servername、client-fingerprint、flow | 固定核心实际检查 YAML 并建立代理连接，才证明生成结果可用 |
| CVR 订阅信息 | [v2.5.8 解析实现](https://github.com/clash-verge-rev/clash-verge-rev/blob/4337bd85758d54d7b9044b2da3c9ac1176eae1f3/src-tauri/src/config/prfitem.rs) 读取 upload/download/total/expire、filename*、按小时的刷新间隔、HTTP/HTTPS 主页 | 名称采用 Content-Disposition filename*；该解析器没有用 Profile-Title 命名。重置信息不冒充 expire，GUI 与实际核心分别记录 |

在线 [V2Ray API 文档](https://sing-box.sagernet.org/configuration/experimental/v2ray-api/) 同样说明该能力不默认包含；固定源码是本基线的主要依据。跨版本文档不能替代目标二进制检查。

## 4. 官方发行资产元数据

以下 SHA256 来自所选版本 GitHub release 资产的 `digest` 字段；**本审计未下载这些资产做独立文件哈希，也未执行它们**。URL、大小和状态保存在 JSON。安装器必须下载后比对完整 SHA256，不能只看版本字符串。

| 资产 | SHA256 | 官方元数据依据 |
| --- | --- | --- |
| sing-box-1.14.3-linux-amd64.tar.gz | `e2bdf179c15a3652955dc44867e33ca0965c9c9b91b34267dcc5a5d639a5feee` | [来源](https://api.github.com/repos/SagerNet/sing-box/releases/tags/v1.14.3) |
| sing-box-1.14.3-linux-arm64.tar.gz | `29dae24d76bea3bc62d07dd3e60cfc12003de7debbd6b768df9be9c68360174e` | [来源](https://api.github.com/repos/SagerNet/sing-box/releases/tags/v1.14.3) |
| mihomo-linux-amd64-compatible-v1.19.32.gz | `ba3ce607747a07f948fc35780e108a4a7c7f552a38b9bd4d115f313ebcb89c20` | [来源](https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/v1.19.32) |
| mihomo-linux-arm64-v1.19.32.gz | `9dd862e28b46ff7d775f169cceebc28deccaa0a9e804237d421cd2571e0caba0` | [来源](https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/v1.19.32) |
| Clash.Verge_2.5.8_x64-setup.exe | `821c0f5957f84c8f7d14532044840e8280d708af24a6a143db360b49bd1e7302` | [来源](https://api.github.com/repos/clash-verge-rev/clash-verge-rev/releases/tags/v2.5.8) |
| nginx-1.30.5.tar.gz | 未取得并核验摘要 | [来源](https://nginx.org/en/download.html) |

Nginx 官方页面提供源码和 PGP 签名链接；这里没有把一个未取得的摘要填入锁文件。实际部署还需固定所选操作系统包修订号或自建产物及摘要。GitHub release digest 是来源证据，不是已完成的下载验收。

## 5. 进入最小闭环的门槛

1. 下载并校验官方 sing-box/Mihomo 资产，分别记录真实版本、架构、构建标签以及配置检查结果。
2. 锁定测试环境 Nginx、OpenSSH、TLS 库及 ACME 客户端包；探测 Nginx 模块、`sshd -t` 和按连接上下文的有效配置。
3. SSH 公钥追加保留旧授权，重复执行幂等，同一密钥限制选项冲突要拒绝；追加后的第二会话登录独立记录。
4. 网站 SNI → Nginx stream → REALITY → 内部 HTTPS 目标：浏览器读到网站正文，客户端完成真实代理请求，分别验收；不能用配置语法通过代替。
5. 订阅正文和响应头作为同版发布；固定 Mihomo 校验生成配置，CVR 2.5.8 真正导入后记录名称、流量、到期、刷新和实际核心版本。
6. 公网 HTTP-01、ACME 自动续期、真实目标 VPS 防火墙、受限公网连通性及 1GB 总资源指标，需要对应目标环境证据；本文件未声称通过。

最小闭环不依赖自动计量。可选计量构建与统计持久化、重置历史在后续阶段加入，但必须保留手工元数据入口并明确其来源。以上未验证项不会被静默转换为成功状态。
