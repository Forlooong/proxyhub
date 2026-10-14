# Copyright and third-party notices

Copyright (C) 2026 ProxyHub contributors.

ProxyHub 原创代码以 **GNU Affero General Public License, version 3 only（AGPL-3.0-only）** 发布。完整标准许可文本见 [LICENSE](LICENSE)，原文取得自 [GNU 官方](https://www.gnu.org/licenses/agpl-3.0.txt)。此授权不替代下面独立组件各自的许可证。

## 原创实现与参考项目

阶段 0–1 的 Python 操作层、SSH 公钥流程、网站/REALITY 配置生成、订阅发布器和 TypeScript 最小生成器为本项目实现。本阶段未将下列六个项目的业务源文件复制进仓库。它们提供需求、行为与架构参考，未来任何实际文件级复用都须另行登记源提交、原始归属、改动和适用许可证。

| 参考来源 | 固定版本的许可与当前使用方式 |
| --- | --- |
| [233boy/sing-box](https://github.com/233boy/sing-box) | GPLv3；参考管理操作覆盖，不执行其安装脚本 |
| [Forlooong/sing-box](https://github.com/Forlooong/sing-box) | GPLv3；参考配置、证书、事务和订阅行为，原项目独立保留 |
| [Forlooong/clash-rules-mrs](https://github.com/Forlooong/clash-rules-mrs) | GPLv3；规则输入数据另有各自授权，本阶段未导入规则数据 |
| [CRThu/clash-rules-mrs](https://github.com/CRThu/clash-rules-mrs) | 审计提交未发现许可；只参考分类需求，不复制脚本 |
| [yuju520/YujuToolBox](https://github.com/yuju520/YujuToolBox) | GPLv3；参考初始化需求，不引入全局命令和安装行为 |
| [SubBoost/subboost](https://github.com/SubBoost/subboost) | AGPL-3.0-only；参考订阅处理边界，本阶段独立实现最小生成器 |

精确提交、许可证文件和核验日期见 [sources.lock.json](evidence/sources.lock.json) 与[来源审计](docs/phase0-sources.md)。表中的 GPLv3 不额外推断 `-only` 或 `-or-later`。

[developer-portfolios](https://github.com/emmabostian/developer-portfolios) 仅作为后续管理面板的设计参考索引；当前未复制其中的作品、图片、字体或网站代码。ProxyHub 与上述项目没有隶属、认证或背书关系。

## 独立运行时

运行时通过固定官方资产下载并核对 SHA256，在 `/opt/proxyhub` 内独立运行；其二进制不作为本仓库源码的一部分提交。保留上游组件名称用于识别原始组件，不把它们重新声明为 ProxyHub 原创代码。

| 组件 | 固定版本与许可依据 |
| --- | --- |
| sing-box | 1.14.3；[固定 LICENSE](https://github.com/SagerNet/sing-box/blob/7054cac5124465ffdb145870d7c01885566711b1/LICENSE)，GPL-3.0-or-later，并有派生名称/关联限制 |
| Mihomo | 1.19.32；[固定 LICENSE](https://github.com/MetaCubeX/mihomo/blob/88dcbf7f1614a67c3b36b848ee3592dfa92ada36/LICENSE)，GPLv3；以固定文件而非当前仓库标签为准 |
| Nginx | 1.30.5；[固定 LICENSE](https://github.com/nginx/nginx/blob/4556c714d1cfa587f92c7c0365c09209d42d148b/LICENSE)，BSD-2-Clause |
| Node.js | 24.21.0；[官方 LICENSE](https://github.com/nodejs/node/blob/v24.21.0/LICENSE)，包含 Node.js 本身及其附带第三方组件的许可 |
| Python / OpenSSH / OpenSSL / Certbot | 使用目标系统提供的软件；按所用发行包的许可、版权及来源记录管理，不由本项目重新许可 |

Clash Verge Rev 2.5.8 为计划验收的外部 GUI 客户端，按其 [GPLv3 许可](https://github.com/clash-verge-rev/clash-verge-rev/blob/4337bd85758d54d7b9044b2da3c9ac1176eae1f3/LICENSE) 管理；本仓库未附带其安装包。计划中的自建计量内核尚未交付，届时应单独提供准确来源、构建说明及全部适用声明。

## 应用依赖

构建及发布依赖由 `package-lock.json` 固定：

- `yaml` 2.9.1，ISC，Copyright Eemeli Aro。应用归档包含此依赖时保留其 `LICENSE`。
- TypeScript 5.9.3，Apache-2.0；`@types/node` 24.10.0 和 `undici-types` 7.16.0，MIT。它们是开发依赖，当前应用归档不携带这些构建工具。
- 管理面板使用 React / ReactDOM 19.3.0（MIT）与 IBM Plex Sans / Mono（SIL OFL 1.1，`@fontsource` 5.3.0）；对应许可证随 `packages/panel/public` 和构建产物提供。Vite 8.3.4、React Vite 插件与类型包仅用于本地构建。
- 网关 Python 依赖由 `requirements-panel.lock` 全量固定，包括 FastAPI 0.143.0（MIT）、Uvicorn 0.54.0（BSD-3-Clause）。安装使用原始 wheel 并保留各包的 `.dist-info/licenses` 等声明。测试专用 httpx 不进入生产依赖清单。

面板从 [developer-portfolios](https://github.com/emmabostian/developer-portfolios) 收集的作品中参考导航与信息层级，未复制其作品代码、素材或品牌；具体取舍记录在 `packages/panel/DESIGN.md`。

`yaml` 的 ISC 许可原文如下：

> Copyright Eemeli Aro &lt;eemeli@gmail.com&gt;
>
> Permission to use, copy, modify, and/or distribute this software for any purpose
> with or without fee is hereby granted, provided that the above copyright notice
> and this permission notice appear in all copies.
>
> THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
> REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
> FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
> INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS
> OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER
> TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF
> THIS SOFTWARE.

如果另行分发包含运行时的完整安装包，应连同各组件适用的许可证、版权声明及所需源码材料一起提供；这份 NOTICE 本身不替代这些文件。
