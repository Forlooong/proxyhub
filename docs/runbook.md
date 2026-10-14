# 阶段 0–1 操作手册

本手册说明阶段 0–1 的 CLI 基础：单 VPS、单 REALITY 节点、单网站、单订阅。专用 Debian 12 amd64 VPS 已通过[最小闭环实测](phase1-acceptance.md)。`0.2.0a1` 的管理面板安装和访问另见[面板手册](panel.md)；HY2、自动计量和通用生产安装器仍待后续阶段。

下文的 `www.example.test`、`sub.example.test`、文档网段地址、账号和路径均为占位示例。执行前替换为自己的真实值；`.test` 示例域名不能申请公网证书。服务器操作在 Linux root 会话执行，客户端操作在持有 SSH 私钥的本机执行。

## 1. 构建与阶段 0 核验

在构建机检出审核过的项目提交，准备 Python 3.11+ 的独立虚拟环境与 Node 24。进入虚拟环境后运行：

```sh
python -m pip install -e .
npm ci
npm run typecheck
npm run build
python -m pip install pytest
python -m pip install -r requirements-panel.lock httpx==0.28.1
python -m pytest -q
python scripts/verify_runtime.py
python scripts/package_app.py
```

`verify_runtime.py` 从 `evidence/runtime-assets.json` 读取固定官方资产，先验证 SHA256/大小，再执行匹配当前系统和架构的核心。其他架构的 `pending` 不会被本机结果覆盖成通过。GPG 可用时验证 Nginx 源码签名；受限 Windows 环境无法初始化 GPG 时会记录受阻。

应用归档输出到 `.cache/proxyhub-app.tar`，只含允许列表中的应用与依赖，不包括本机 `.local`、证据缓存、SSH 私钥或部署配置。版本来源与签名链见[阶段 0 运行时](phase0-runtime.md)和[启动运行时验证](bootstrap-verification.md)。

帮助可在未初始化的环境运行：

```sh
proxyhub help
proxyhub help host append-key
proxyhub help site certificate issue
proxyhub help deployment plan
proxyhub sub urls --help
```

目前没有 `help all`、`help legacy` 或完整模块命令集；后续阶段扩展同一命令结构。命令末尾的 `--help` 和 `proxyhub help <层级>` 都不会触发服务器操作。

## 2. 准备专用 Linux 验收机

当前 bootstrap 只接受 Debian 12 amd64。需要 systemd、Python 3.11+、OpenSSH 服务、OpenSSL、Certbot、`dpkg-deb` 以及 Nginx 软件包所需系统库。软件包依赖以已核验 DEB 元数据为准。不要把它当作 Ubuntu、arm64 或旧部署迁移安装器。

先核对：

- 两个域名都指向目标 VPS；若设置 AAAA，IPv6 也须真实可达。
- TCP 80/443 没有被现有服务占用，安全组/防火墙允许 HTTP-01 和 HTTPS。程序不会抢占已有端口。
- `/opt/proxyhub`、`/var/lib/proxyhub`、`proxyhub-*` 用户/服务和 `/usr/local/bin/proxyhub` 没有无关资源冲突。
- 已有 SSH 会话保持连接，账号、当前 SSH 端口和救援入口已核对。

将相同审核提交的源码目录及已构建归档上传到测试机。以下假设源码位于 `/root/proxyhub-source`，归档位于 `/root/proxyhub-app.tar`：

```sh
cd /root/proxyhub-source
python3 scripts/bootstrap_test_host.py --install
install -d -m 0755 /opt/proxyhub/app
tar -xf /root/proxyhub-app.tar -C /opt/proxyhub/app
PYTHONPATH=/opt/proxyhub/app/src python3 -m proxyhub install-services
```

bootstrap 下载并核对锁定运行时，只解包到 `/opt/proxyhub`。`install-services` 创建自己的系统账号、服务定义和 `proxyhub` 命令，执行 `daemon-reload`，**此时不会启动代理或网站**。不要手工创建 `.proxyhub-owned` 来绕过已有目录所有权检查。

| 路径 / 服务 | 用途 |
| --- | --- |
| `/opt/proxyhub/app` | 应用与构建好的生成器 |
| `/opt/proxyhub/bin` | 项目内部使用的固定运行时入口 |
| `/var/lib/proxyhub/plans` | 受限 SQLite 计划与激活日志 |
| `/var/lib/proxyhub/revisions` | 不可变候选版本，含私有配置 |
| `/var/lib/proxyhub/current` | 当前版本符号链接 |
| `/var/lib/proxyhub/certificates` | 版本化证书与私钥 |
| `/var/lib/proxyhub/publish` | 只读发布器消费的完整 bundle |
| `proxyhub-proxy.service` | 低权限 sing-box |
| `proxyhub-publisher.service` | 低权限、仅 loopback 的订阅发布器 |
| `proxyhub-nginx.service` | 独立 Nginx 公网入口与网站 |

内部 `sing-box`、`nginx` 等入口不会加入或覆盖全局 PATH。已有项目仍由各自原命令管理。

## 3. 追加 SSH 公钥并验证第二会话

将现有公钥的 `.pub` 文件放到服务器受限路径，例如 `/root/operator.pub`。只需公钥；私钥留在客户端。目标账号必须已经存在。

OpenSSH 的 `Match` 条件会影响实际授权文件。核对预期第二会话的客户端源地址、客户端主机名、服务器接收地址和 SSH 端口，再查看生效配置。例如：

```sh
/usr/sbin/sshd -T -C user=operator,addr=203.0.113.10,host=client.example.test,laddr=192.0.2.20,lport=22
```

从 `authorizedkeysfile` 生效结果选择正确文件，不能假定总是 `/home/operator/.ssh/authorized_keys`。`--addr` 是服务器看到的客户端源地址；`--host` 用于 `Match Host`；`--laddr/--lport` 是该连接的服务端地址/端口。NAT 后的地址以服务端实际连接为准。

若当前禁用了公钥认证，可单独预览、启用该项：

```sh
proxyhub host enable-key-auth
proxyhub host enable-key-auth --apply
```

此命令只尝试开启公钥认证，检查后重载 SSH；密码、端口及 root 策略保持原值。账号专属 `Match` 或自定义 SSH 布局仍须按实际生效结果处理。

准备追加计划：

```sh
proxyhub host append-key \
  --account operator \
  --public-key-file /root/operator.pub \
  --authorized-keys-file /home/operator/.ssh/authorized_keys \
  --addr 203.0.113.10 \
  --host client.example.test \
  --laddr 192.0.2.20 \
  --lport 22

proxyhub apply 'PLAN_ID_FROM_PREVIOUS_COMMAND'
```

计划输出包含账号、目标、指纹与计划 ID。应用前会重新核对生效配置和原文件快照；已有字节保留，写入前创建受限备份，新内容经原子替换。相同公钥且限制等效时不重复追加；同一公钥带不同限制选项会拒绝，不能用一条无限制授权绕过原限制。未知选项、不安全权限、符号链接或并发变化也会拒绝。

写入返回的 `verification: unverified` 是预期状态，文件成功写入不等于登录成功。保持原会话，在已核验服务器主机密钥的客户端开第二会话：

```sh
ssh -o StrictHostKeyChecking=yes \
  -o IdentitiesOnly=yes \
  -o PreferredAuthentications=publickey \
  -o PasswordAuthentication=no \
  -i ~/.ssh/id_ed25519 \
  -p 22 operator@server.example.test true
```

退出码 0 才证明这次独立会话通过密钥认证。本阶段没有关闭密码、收紧 root、修改 SSH 端口或防火墙的自动流程。重复追加验证应重新准备计划，不能复用已经应用的旧计划 ID。

## 4. 申请或导入网站证书

初次 HTTP-01 申请要求公网 TCP 80 可达，且本机 IPv4/IPv6 的 80 端口空闲：

```sh
proxyhub site certificate issue \
  --domain www.example.test \
  --domain sub.example.test
```

命令使用 Certbot standalone，申请同时覆盖网站与订阅域名的证书，复制到版本化受管目录。当前调用使用非交互方式接受 ACME 服务条款，不设置联系邮箱。若需其他 ACME 账号策略，应先完成适配。

已有有效证书也可以导入：

```sh
proxyhub site certificate import \
  --certificate /root/certificate-input/fullchain.pem \
  --key /root/certificate-input/privkey.pem \
  --domain www.example.test \
  --domain sub.example.test
```

导入会检查证书与私钥匹配、域名和有效期；信任链由后续 TLS 健康检查与真实客户端访问核验。导入成功不意味着自动续期。自签名证书不作为公网验收通过依据。保存命令返回的受管 `certificate_path` 与 `key_path`，下一步用这两个路径。

本阶段未提供 DNS-01、泛域名自动申请和证书依赖管理页面。安装器会启用独立续期 timer，首次部署成功后按第 7 节启动。

## 5. 准备并启用网站、REALITY 和订阅

以下 `CERTIFICATE_VERSION` 必须替换为证书命令实际返回的版本目录。初始配置自动生成新的 UUID、REALITY 密钥和高熵订阅令牌，写入私有文件，不在命令参数中要求传入这些秘密：

```sh
proxyhub deployment init \
  --site-domain www.example.test \
  --subscription-domain sub.example.test \
  --public-address server.example.test \
  --certificate /var/lib/proxyhub/certificates/CERTIFICATE_VERSION/fullchain.pem \
  --key /var/lib/proxyhub/certificates/CERTIFICATE_VERSION/privkey.pem \
  --output /root/proxyhub-config.json

proxyhub deployment plan --config /root/proxyhub-config.json
proxyhub apply 'PLAN_ID_FROM_PREVIOUS_COMMAND'
```

`init` 拒绝覆盖已存在的输出。`plan` 生成独立版本，校验证书，并实际执行固定版本 sing-box、Nginx、Mihomo 配置检查。`apply` 重新核验候选文件和证书未被改动，再切换版本、发布订阅、启动并检查自己的服务。成功后启用这些服务的开机启动。

默认流向：公网 TCP 443 的网站 SNI → 本机 REALITY `2443` → 本机 HTTPS 网站 `8443`；订阅 SNI → 本机 HTTPS `8444` → 只读发布器 `18890`。普通浏览器与 REALITY 客户端共用网站 SNI，由 REALITY 处理认证与普通 HTTPS 目标转发。HTTP-01/跳转使用 TCP 80。

应用返回 `runtime_health: passed` 仍需要下面的真实客户端验收。其内置健康检查不代替穿过公网 SNI 入口的真实代理请求。

## 6. 私有订阅配置与客户端导入

先把链接写到私有文件，避免显示到共享日志：

```sh
proxyhub sub urls --output /root/proxyhub-client-links.json
```

文件包含 Mihomo YAML 与 URI/Base64 raw 两个 HTTPS 链接。完整 URL 是访问凭据，仅通过自己的受保护会话复制到客户端。`proxyhub sub urls` 不带 `--output` 会在终端输出完整秘密链接。

在客户端中导入 Mihomo YAML 链接后，分别记录 GUI 版本与实际内核版本。当前核心基线为 Mihomo 1.19.32，GUI 计划基线为 Clash Verge Rev 2.5.8；不能只看 GUI 版本推断内核。验证下载后加载配置，再发起一个真实代理请求，确认请求经过该节点。raw 输出是共享 URI 的标准 Base64，只有支持对应 VLESS/REALITY 参数的客户端才能使用。

当前配置为一个明确的 `PROXY` 选择组、最终 `MATCH,PROXY`；不开 LAN、不启用 TUN，没有国内/国外规则分流。生成器建立独立的节点 DNS 解析路径。GUI 对名称、流量、到期及刷新间隔的显示仍需单独验收；本阶段没有二维码和一键导入按钮。

可在保留权限的私有编辑器中修改 `/root/proxyhub-config.json`，然后重新 `deployment plan`、`apply`：

| 字段 | 当前边界 |
| --- | --- |
| `server.site_title` / `server.node_name` | 默认网站标题与节点名称；不是自定义网站内容上传 |
| `subscription.name` | 下载文件名与支持客户端的订阅名称来源 |
| `subscription.userinfo` | 可为空表示未知；`upload/download/total` 必须一并给出，单位为字节；`expire` 为独立可选 Unix 秒时间戳 |
| `subscription.update_interval_hours` | 1–168 小时，默认 6；建议客户端刷新间隔 |
| `subscription.reset_day/reset_at` | 管理元数据，当前不执行重置，也不伪装成到期时间 |
| `overrides` | 仅允许 `mixed-port`、`log-level`、`mode`、`ipv6`、`allow-lan`；`allow-lan` 只能为 `false` |

流量信息是手工填写；未知额度保留空对象，不能用零冒充已采集数据。系统生成的 `proxies`、`proxy-groups`、`rules`、DNS 和其他字段不允许从覆盖层替换。不直接编辑 `current/client.yaml` 或发布 bundle；下一次生成会使用私有配置中的覆盖层。

发布器严格匹配 Host 和路径，仅支持 GET/HEAD，没有远程抓取或写接口。每次响应的正文和元数据来自同一 bundle；重载失败保留旧快照。停止 CLI/管理操作不影响发布进程提供最后有效版本。当前令牌可以通过修改私有配置后重新发布进行更换，尚无专用撤销/轮换命令；旧节点凭据不会因为订阅链接变化而失效。

## 7. 续期、状态与恢复

当前 HTTP-01 受管证书可手动执行以下续期入口：

```sh
proxyhub deployment renew
```

它利用已经运行的网站 webroot 验证；未到续期时间且证书未变时返回 unchanged，证书变化后重新准备部署并应用。需保持 TCP 80 可达。仅 `site certificate issue` 创建、且来源与域名匹配的受管证书参与续期；导入的外部证书返回 skipped，由其原始签发流程续期，再导入并部署。续期期间若配置发生变更，会拒绝应用旧配置。

安装器启用 `proxyhub-renew.timer`，首次部署成功后启动它：

```sh
systemctl start proxyhub-renew.timer
systemctl list-timers proxyhub-renew.timer
```

每天服务器本地时间 04:20 加最多一小时随机延迟执行；停机期间错过的任务会补执行。使用自己的 ACME 路径，不依赖系统默认 Certbot timer。

只查看本项目服务：

```sh
systemctl status proxyhub-proxy.service proxyhub-publisher.service proxyhub-nginx.service
journalctl -u proxyhub-proxy.service -u proxyhub-publisher.service -u proxyhub-nginx.service --since today
```

CLI 普通错误不回显完整底层配置或订阅内容，原始运行时日志仍属于管理员诊断材料，公开前脱敏。

正常应用失败会尝试恢复上一有效版本；首次部署失败没有旧版本可恢复时停止自己的服务。进程在激活中断后，先执行：

```sh
proxyhub deployment recover
```

恢复入口撤回未完成激活，不继续执行中断的候选。计划过期、已执行、内容被改动或恢复未完成时，应按错误原因处理后重新准备计划，不能绕过状态检查直接替换 `current`。

SSH 追加的备份路径由 apply 结果给出。需要撤回时，先检查追加后是否还有其他人修改了授权文件，保留当前连接，并按原属主/权限恢复；本阶段没有自动 SSH 回滚命令。跨机器备份恢复、旧部署迁移、域名退役与防止旧令牌复活的统一恢复流程仍属于后续阶段。

## 8. 最小闭环验收

按顺序保存脱敏结果，不将配置校验等同于网络成功：

1. 原 SSH 授权字节保留；新公钥第二会话登录成功；重复追加不增加条目；限制选项冲突被拒绝。
2. 浏览器通过自有网站域名正常校验 HTTPS 并获得默认页面。
3. 从公网订阅域名下载配置；错误 token、错误 Host 和未知路径被拒绝，GET/HEAD 的元数据一致。
4. 锁定客户端内核加载下载的配置，发起真实 REALITY 代理请求；网站仍可正常访问。
5. 修改允许的客户端覆盖/手工元数据后重新发布，覆盖仍保留，正文和响应头版本一致。
6. 候选校验失败或模拟激活故障时保留上一有效服务，恢复日志可核对。

GUI 显示兼容、Linux arm64/Ubuntu 安装、完整防失联流程和 1 核 1GB 压力测试需后续独立验收。证书更新、回滚与崩溃恢复的实测结果见阶段 1 记录。公开记录仅保存版本、配置摘要、时间和结果；不得包含服务器实际身份、私钥、节点凭据或完整订阅链接。
