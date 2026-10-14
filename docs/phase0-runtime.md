# 阶段 0：版本、资产与可执行能力核验

核验日期：2026-10-14。此记录只陈述已经运行的检查；配置校验通过不等同于真实代理请求、GUI 导入或 Linux 服务验收通过。

后续补证：同日已完成 Debian 12 amd64 的运行时启动、配置校验及公网最小闭环，见[Linux 验收证据](../evidence/linux-acceptance.json)与[阶段 1 验收](phase1-acceptance.md)。下表保留最初 Windows 核验的范围，不能据此推断 arm64 或 GUI 已验收。

## 已取得的结果

| 组件 | 锁定版本 | 资产来源与完整性 | 实际执行结果 |
| --- | --- | --- | --- |
| sing-box | 1.14.3 | 官方 Release API 的 Linux amd64、Linux arm64、Windows amd64 资产均存在；三份完整下载的 SHA256 和大小与官方 API 相符 | Windows amd64 `version`、本机 HTTPS target 的 VLESS/REALITY 配置 `check` 通过 |
| Mihomo | 1.19.32 | 官方 Release API 的 Linux amd64-compatible、Linux arm64、Windows amd64-compatible 资产均存在；三份完整下载的 SHA256 和大小与官方 API 相符 | Windows amd64 `-v`、最小订阅配置 `-t` 通过 |
| Nginx | 1.30.5 | 官方下载页列为 stable；源码归档完整下载，版本头匹配，官方分离签名验证通过 | Linux 构建、`nginx -V` 模块检查和实际 SNI 转发待独立验收 |
| Clash Verge Rev | 2.5.8 | 采用设计文档中锁定的客户端 GUI 版本 | 本脚本没有启动 GUI，订阅导入及响应头显示待独立验收 |

sing-box 实际输出的 revision 为 `7054cac5124465ffdb145870d7c01885566711b1`，Go 为 `go1.26.8`。默认二进制标签不含 `with_v2ray_api`；向配置加入 V2Ray API 后，`check` 以退出码 1 拒绝，并明确要求该构建标签。因此首个闭环使用手工元数据；自动计量必须使用另行构建并核验过的内核，不能把当前发布包标为支持真实流量采集。

Nginx 源码 SHA256 为 `6c20565aa2325cb82216ae804f4a4ff1875179014759a381c42ddc8e11c4906d`；有效签名指纹为 `D6786CE303D9A9022998DC6CC8464D549AF75C0A`。签名使用从 Nginx 官方 HTTPS 公钥页取得的公钥核验，未宣称建立独立的公钥信任链。本地 GPG 使用项目缓存内独立 keyring，未操作用户默认 keyring。

## 证据与复核方式

- `evidence/runtime-assets.json` 是资产输入清单，字段包含 `schema_version`、`checked_at`、`source` 和 `assets[]`。每项固定 `project`、`version`、`platform`、`name`、`url`、`sha256`、`size`、`release_url`；哈希来自官方 Release API。
- `evidence/runtime-verification.json` 保存运行时间、执行平台、逐资产实测哈希和大小、逐检查退出码及输出、Nginx 签名结果、未覆盖范围。`passed`、`pending`、`blocked`、`failed` 分开保存。
- `scripts/verify_runtime.py` 只下载到 `.cache/runtime`，校验哈希后才提取和执行匹配当前操作系统/架构的可执行文件；不安装服务、不修改 PATH、不覆盖原命令。它逐个复制已知可执行文件，避免直接展开归档中的任意路径。

```sh
python scripts/verify_runtime.py
python scripts/verify_runtime.py --metadata-only
```

`--metadata-only` 的含义为下载并校验资产、跳过 sing-box/Mihomo 执行；Nginx 有 GPG 时仍校验签名。离线缓存已有正确资产时可复用，但 Nginx 分离签名仍从官方 URL 取回。重复运行会更新本次运行证据；CI 应把各平台证据分别归档，不将一台 Windows 的结果当作所有平台结果。

Git for Windows 的 GPG 在受限 MSYS 环境中可能无法初始化；脚本会记录 `blocked`，不得用此状态宣称签名失败或版本不存在。本次使用项目内独立 keyring 已完成有效签名校验。

## Linux 与业务验收边界

Linux amd64/arm64 已核验的是下载资产，而非在目标架构上启动后的行为。Nginx 需要 `stream`、`stream_ssl_preread`、HTTP TLS 能力；发行版的动态模块还需要核对加载方式，不能仅以源码存在判定已启用。下列事项仍由阶段 1 的环境验收覆盖：

- Linux SSH 的有效 `AuthorizedKeysFile`、账号权限、第二会话密钥登录及回滚；Windows 配置检查不能代替这些操作。
- 自有域名的 DNS 与 HTTP-01 可达性、证书签发/续期、真实浏览器访问、真实 REALITY 代理请求。
- 下载订阅后由锁定的客户端内核加载，再用锁定 GUI 核验名称、额度、到期与刷新间隔。
- Linux 1 核 1GB 环境下的内存、重启恢复、服务权限与资源成本。

本脚本的最小 Mihomo 配置不包含真实节点，只验证内核及最小配置解析可用；真实节点导入和流量闭环由项目集成验收承担。

## 官方来源

- [sing-box v1.14.3 Release](https://github.com/SagerNet/sing-box/releases/tag/v1.14.3)，[Release API](https://api.github.com/repos/SagerNet/sing-box/releases/tags/v1.14.3)
- [Mihomo v1.19.32 Release](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.32)，[Release API](https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/v1.19.32)
- [Nginx 官方下载](https://nginx.org/en/download.html)，[公钥页](https://nginx.org/en/pgp_keys.html)
- [sing-box V2Ray API 文档](https://sing-box.sagernet.org/configuration/experimental/v2ray-api/)
