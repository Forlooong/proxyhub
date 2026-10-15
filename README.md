# ProxyHub

独立、模块化的 VPS 与代理服务管理项目。提供单节点命令行和管理面板，再逐步加入规则管理与更多协议。

当前版本为 `0.3.0a1`：SSH 公钥追加、自有 HTTPS 网站与 VLESS REALITY 共存、Mihomo YAML 和原始订阅发布，并通过私有管理面板进行预览、应用与任务追踪。本次阶段 2a 加入单站点静态内容上传、隔离预览和成功应用版本恢复。完整产品范围见[已批准设计](docs/superpowers/specs/2026-10-14-proxyhub-design.md)。

## 当前能力

- **SSH**：针对账号和实际连接条件读取 OpenSSH 生效配置；保留原授权项，检查重复公钥和限制选项冲突，准备计划后备份并原子追加。登录是否成功由第二个独立 SSH 会话验证。
- **网站与 REALITY**：生成默认静态网站或上传静态 ZIP，支持隔离预览、内容版本列表和恢复已成功应用的版本/默认模板。以 Nginx SNI 分流共用 TCP 443；REALITY 握手目标固定为本机内部 HTTPS 网站，避免公网回环。证书可通过 HTTP-01 申请或导入，按版本保存。
- **订阅**：由唯一 TypeScript 生成器输出一个 REALITY 节点的 YAML 和 URI/Base64；正文、响应头和访问路径作为同一发布版本。手工流量信息与重置字段分别保存。
- **变更**：准备不可变候选、执行核心配置检查、显式 `apply`；保留激活日志和失败恢复入口。独立发布进程提供最后一份有效订阅。
- **命令**：仅新增 `proxyhub` 和 `proxyhub-*` 服务；不覆盖 `sb`、`sing-box`、`yuju`、`subboost` 或 shell 内置 `help`，端口及资源冲突会停止操作。
- **面板**：经 SSH 隧道访问，低权限网关与 root 管理器分离；支持网站内容上传/预览/恢复、默认模板标题、证书导入/续期、节点名称/地址、订阅元数据与配置覆盖、公钥追加。变更需先预览，旧版本计划不可覆盖新配置，见[面板手册](docs/panel.md)。

当前安装脚本只面向 **Debian 12 amd64、systemd、root** 的专用验收环境。帮助与纯配置测试可在 Windows/Linux 运行。Ubuntu、其他发行版和 arm64 的完整安装验收尚未完成。

## 验证状态

| 范围 | 状态与证据 |
| --- | --- |
| 六个来源项目、许可证、固定提交 | 已核对，[来源与取舍](docs/phase0-sources.md) |
| sing-box 1.14.3 / Mihomo 1.19.32 官方资产 | 六份 Linux/Windows 资产的哈希与大小通过；Windows 可执行能力检查通过，[运行时核验](docs/phase0-runtime.md) |
| Nginx 1.30.5 / Node 24.21.0 | 官方签名和哈希链通过，[启动运行时核验](docs/bootstrap-verification.md) |
| SSH 第二会话、网站与真实 REALITY 请求、下载订阅使用 | 专用 VPS 实测通过，[阶段 1 验收](docs/phase1-acceptance.md) |
| 管理面板与四类操作、订阅独立发布、管理服务停止后的公网闭环 | 专用 VPS 与真实浏览器实测通过，[面板阶段验收](docs/phase2-panel-acceptance.md) |
| 阶段 2a 单站点上传、隔离预览与内容恢复 | 234 项 Linux 相关测试、真实浏览器与公网代理请求通过，见[阶段 2a 验收记录](docs/phase2a-content-acceptance.md) |

配置检查不代表真实客户端请求通过。Clash Verge Rev 2.5.8 的 GUI 导入与显示、1 核 1GB 资源指标仍待专门验收。

## 开始使用

需要 Python 3.11+；构建订阅生成器需要 Node 24 和 npm。先创建并进入 Python 虚拟环境，再执行：

```sh
python -m pip install -e .
proxyhub help
proxyhub help host append-key
proxyhub deployment plan --help

npm ci
npm run build
python -m pip install pytest
python -m pip install -r requirements-panel.lock httpx==0.28.1
python -m pytest -q
```

`proxyhub help`、嵌套 `help` 和 `--help` 不要求 root、不创建状态、不访问网络。安装与操作 Linux 服务器的完整步骤见[阶段 0–1 操作手册](docs/runbook.md)；现阶段没有通用的一键生产安装器。

## 单站点网站内容

上传包须为不超过 **180 KiB** 的 ZIP，实际解压总量不超过 **2 MiB**、文件和目录合计不超过 **200 项**，根目录包含非空 UTF-8 `index.html`。只接受白名单中的 HTML、CSS、JS、JSON、文本、图片和字体扩展，完整列表见[面板手册](docs/panel.md#网站内容上传预览与恢复)。隔离预览 HTML 上限为 **512 KiB**，禁用脚本与外部资源，CSS `url()` 和依赖 JavaScript 的效果可能无法完整呈现。公网网站仅允许同源外置 `.js` 脚本，内联脚本和外部资源不放行。

以下命令需 Linux root。上传和恢复均先输出计划，检查预览后再显式 `apply`；恢复默认模板时将版本 ID 替换为 `template`：

```sh
proxyhub site content upload --archive /root/website.zip
proxyhub site content preview --plan-id <PLAN_ID> --output /root/website-preview.html
proxyhub apply <PLAN_ID>
proxyhub site content versions
proxyhub site content restore --version <CONTENT_VERSION_ID>
# 对恢复命令返回的新计划，再执行 preview / apply。
```

仅已成功应用的内容版本可恢复。上传和恢复沿用当前域名、证书、REALITY 节点与订阅；完整重载可能短暂中断服务，后续订阅或证书变更保留上传内容。网站标题仅在默认模板下可编辑。CLI 预览文件以 `0600` 权限写入，不应加入仓库，查看后按需删除。

## 端口与路径

```mermaid
flowchart LR
    A[公网 TCP 443 · Nginx] -->|网站 SNI| R[127.0.0.1:2443 · REALITY]
    R -->|认证代理请求| P[代理出站]
    R -->|握手目标 / 普通 HTTPS| W[127.0.0.1:8443 · 网站]
    A -->|订阅 SNI| T[127.0.0.1:8444 · 订阅 TLS]
    T --> S[127.0.0.1:18890 · 只读发布器]
```

TCP 80 用于 HTTP-01 和 HTTPS 跳转。安装目录为 `/opt/proxyhub`，私有状态位于 `/var/lib/proxyhub`；新命令为 `/usr/local/bin/proxyhub`。运行时的内部命令路径只在 `/opt/proxyhub/bin` 使用，不替换全局同名程序。

## 后续阶段

多站点、域名迁移、HY2 生命周期、外部 REALITY 目标、多订阅、MRS 规则流水线、共享流量账户、自动采集与周期重置仍按设计分阶段实施。当前默认 sing-box 包不含 V2Ray API，因此没有把手工额度显示标为自动计量；当前 `reset_day/reset_at` 也不会触发自动重置。

ProxyHub 原创代码采用 [AGPL-3.0-only](LICENSE)。参考来源、独立运行时和依赖的归属见 [NOTICE](NOTICE.md)。公开仓库不保存真实服务器身份、SSH 私钥、节点凭据或完整订阅链接。
