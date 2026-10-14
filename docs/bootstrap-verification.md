# 启动运行时：发行签名与哈希链核验

核验时间：2026-10-14；机器可读结果见 `evidence/bootstrap-signatures.json`。本次只在本地项目缓存中读取官方元数据、验证签名与下载一份约 1MB 的 Nginx DEB，没有连接 VPS、安装软件或修改系统密钥库。

## Nginx 1.30.5：完整软件源签名链通过

核验对象为 Debian 12 / amd64 的 `nginx_1.30.5-1~bookworm_amd64.deb`，链路为：

```text
官方 nginx_signing.key
  → GPG 验证 InRelease
  → 经过签名验证的 Release 中的 SHA256 和大小
  → Packages.gz
  → nginx / 1.30.5-1~bookworm / amd64 的 SHA256 和大小
  → 本次独立下载的 DEB
```

| 检查 | 结果 |
| --- | --- |
| 公钥来源 | `https://nginx.org/keys/nginx_signing.key` |
| 官方文档标注的主指纹在公钥包中存在 | `573BFD6B3D8FBC641079A6ABABF5BD827BD9BF62` |
| 本份 InRelease 实际有效签名指纹 | `8540A6F18833A80E9C1653A42FD21310B49F6B46`，属于同一官方公钥包 |
| InRelease 日期 | 2026-09-15 15:59:55 UTC；签名有效 |
| Packages.gz SHA256 | `d16e7feb2f45f15c6d4577afa6d631dd43a39c9ce43520e0ce7e2a19b5287cba` |
| Packages.gz 大小 | 47,962 字节，与已验签 Release 一致 |
| DEB SHA256 | `ab248710dbd9c93dcd2eeb2df0544f3612011ec9fe96c396076a73d2421304bd` |
| DEB 大小 | 1,085,720 字节，与已核验 Packages 一致 |
| 与项目锁定清单一致 | 是，版本、架构、下载 URL、SHA256 全部一致 |

Nginx 官方文档注明公钥文件可以包含其他用于签名的软件包密钥，所以文档示例主指纹与本次签名指纹分别记录。本份 Release 未包含 `Valid-Until`，不能从签名有效推导“元数据必然最新”；本次核验的是项目明确锁定的版本及内容链。

## Node.js 24.21.0：签名校验清单通过

使用官方 `nodejs/release-keys` 仓库固定提交 `481637f813e912c4aa3622d7964ab426c97b8e8d` 中的 active release keys，在项目缓存内独立 keyring 验证 `SHASUMS256.txt.sig`。没有从任意密钥服务器自动取键。

| 检查 | 结果 |
| --- | --- |
| 实际有效签名指纹 | `5BE8A3F6C8A5C01D106C0AD820B1A390B168D356` |
| 签名者身份依据 | 固定提交的 active release keys 名单中 Antoine du Hamel 的主指纹 |
| 签名日期 | 2026-09-08 |
| SHA256 清单自身的 SHA256 | `f410428039e2c922a14058df067a4482691c9304a5c01a75847f9f3f2d3307f6` |
| 清单中的归档 | `node-v24.21.0-linux-x64.tar.xz` |
| 归档预期 SHA256 | `fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6` |
| 与项目锁定清单一致 | 是 |

本次独立验证确认了预期归档哈希的签名来源；Node 大归档没有重复下载。归档实际哈希和 Linux 执行结果由测试 VPS 的独立验收记录承担，不将本记录冒充为第二次归档下载实测。

## 可复核性与信任边界

机器可读记录保存官方 URL、指纹、签名日期、各层 SHA256、大小、固定提交和本次执行时间。相关元数据、公钥与独立 keyring 缓存在 `.cache/runtime/bootstrap-signatures`；它们不属于发布运行时。核验顺序必须是先验证签名，再读取经过验签的内容并校验下一层哈希，最后比对项目版本锁。

GPG 必须同时正常退出并产生 `VALIDSIG`，出现无效、过期或撤销签名不能记为通过。公钥从官方 HTTPS 站点和官方 GitHub 仓库取得；本记录没有宣称完成独立 Web of Trust 验证、透明日志证明或外部时间戳背书。

此次结果补充 `evidence/bootstrap-assets.json` 中标记为 pending 的签名证据，保留原资产清单不变。安装时仍应使用锁定 SHA256 校验实际下载文件，不能因为本次元数据签名通过而跳过每次安装的内容校验。

## 官方依据

- [Nginx 官方 Linux 软件包说明](https://nginx.org/en/linux_packages.html)
- [Nginx Debian bookworm InRelease](https://nginx.org/packages/debian/dists/bookworm/InRelease)
- [Nginx Debian bookworm amd64 Packages.gz](https://nginx.org/packages/debian/dists/bookworm/nginx/binary-amd64/Packages.gz)
- [Node v24.21.0 SHA256 清单](https://nodejs.org/dist/v24.21.0/SHASUMS256.txt) 与 [分离签名](https://nodejs.org/dist/v24.21.0/SHASUMS256.txt.sig)
- [Node 官方签名密钥与操作说明，固定提交](https://github.com/nodejs/release-keys/tree/481637f813e912c4aa3622d7964ab426c97b8e8d)
