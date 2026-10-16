# Managed subscription converter

This independently implemented worker is the single client configuration
generator. It preserves the original VLESS REALITY TCP/Vision node and optional
managed Hysteria2 node, then appends enabled local protocol snapshots supplied
by the manager. It does not import external URIs or subscriptions or fetch rules.

Build with Node 24:

```sh
npm ci
npm run build
node packages/converter/dist/worker.js < input.json > output.json
```

The worker reads one UTF-8 JSON document, at most 64 KiB, from stdin. Successful
stdout is one JSON document containing `yaml`, `raw`, `uri`, `uris`, and `proxies`. `uri`
remains the REALITY URI for existing callers. `uris` contains REALITY first,
then Hysteria2 when supplied, then the supplied `nodes` in order. `proxies` is
the exact generated Mihomo proxy array, also consumed by runtime acceptance;
it contains client credentials and must not enter public logs. `raw` is standard Base64 of those URI lines,
each ending with a newline. Invalid, unknown, duplicate, or unsupported
input fields cause a nonzero exit and a fixed error message on stderr. The
worker performs no file writes or network requests.

Input fields:

```json
{
  "node": {
    "id": "stable-node-id",
    "name": "Visible node name",
    "server": "proxy.example.test",
    "port": 443,
    "uuid": "1a234567-1234-4234-8234-123456789abc",
    "server_name": "www.example.test",
    "public_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    "short_id": "aabbccdd"
  },
  "overrides": {
    "mixed-port": 17890,
    "log-level": "warning",
    "mode": "rule",
    "ipv6": false,
    "allow-lan": false
  }
}
```

The sample is a public validation fixture, not a working proxy. The
`overrides` object is optional. Those five keys are the complete allowlist;
`allow-lan` may only be `false`. The mixed port must be 1–65535, mode must be
`rule`, `global`, or `direct`, and log level must be `silent`, `error`,
`warning`, `info`, or `debug`. Every explicitly supplied value is validated.
Defaults disable LAN and TUN, bind the mixed listener to loopback, and enable
one select group named `PROXY` with `MATCH,PROXY`. A mode override can change
the client's routing mode. DNS uses literal-IP encrypted resolvers and an
independent node-domain resolver with `respect-rules: false`; it does not rely
on an already-established proxy to bootstrap node DNS.

`public_key` is a canonical unpadded Base64url 32-byte public key; `short_id`
is an even-length hexadecimal string of 0–16 characters. The private REALITY
key never enters the converter. Unicode node names are supported; reserved
group/builtin names and commas are rejected.

An optional top-level `hy2` object has exactly `id`, `name`, `server`, `port`,
`password`, and `server_name`. Its stable ID is `local-hy2`, name must differ
from the REALITY name, password must be 32–128 URL-safe ASCII characters,
and SNI must be a non-local DNS name. The generated Mihomo node uses
`type: hysteria2`, `password`, `sni`, and `skip-cert-verify: false`; it has no
bandwidth, obfuscation, insecure TLS, or advanced overrides. Its URI is
`hysteria2://<password>@<server>:<port>/?sni=<name>#<label>` with encoded
credentials and label, and bracketed IPv6 authority when needed. The `PROXY`
select group retains REALITY as its first default choice. A disabled managed
resource is omitted by Python before invoking the worker; explicit `hy2: null`
is rejected.

An optional `nodes` array contains at most 32 enabled managed resources. Each
snapshot has exactly `id`, `protocol`, `name`, `server`, `port`, `options`, and
`credentials`. IDs are 32 lowercase hexadecimal digits; names and IDs must be
unique across the original node, HY2, and extra nodes. The manager normalizes
defaults before invocation; the worker validates every field independently.
Unknown or incompatible fields are rejected, including private keys, arbitrary
Mihomo options, unsupported transports, and insecure TLS overrides.

| Protocol | Accepted options and credentials |
| --- | --- |
| `ss` | `options.method`: `aes-128-gcm`, `aes-256-gcm`, `chacha20-ietf-poly1305`, or their three `2022-blake3-*` methods; `credentials.password`. No plugins. |
| `socks5` | `options.auth` and `options.udp` booleans. Credentials are `username`/`password` when authenticated and `{}` otherwise. The protocol is unencrypted. |
| `trojan` | Certificate TLS, `tcp`/`ws`/`httpupgrade`; `credentials.password`. H2 is rejected because the pinned client has no Trojan H2 transport. |
| `vmess` | `tcp`/`ws`/`httpupgrade` with plain or certificate TLS; H2 with TLS; HTTP/1 headers only with plain `security: none`. `security` is `auto`/`none`/`aes-128-gcm`/`chacha20-poly1305`; `credentials.uuid`; generated `alterId` is always zero. |
| `vless` | `tcp`/`ws`/`httpupgrade` with plain or certificate TLS; H2 with TLS. `flow` is empty or TCP/TLS-only `xtls-rprx-vision`. REALITY permits TCP/H2 only. `credentials.uuid`, plus `public_key`/`short_id` for REALITY. |
| `tuic` | Certificate TLS; `congestion_control`: `bbr`/`cubic`/`new_reno`, `udp_relay_mode`: `native`/`quic`; `credentials.uuid`/`password`. This is v5, never a converted v4 token. |
| `anytls` | Certificate TLS; `credentials.password`. No REALITY or HTTP transport. |

Stream protocols explicitly supply `transport` and `tls`. Non-TCP transports
also supply one DNS `host` and an ASCII `path` beginning with `/`, without query
or fragment. TCP rejects those HTTP fields. TLS options are `tls: true`,
`tls_mode: certificate`, `sni`, and `certificate_id`. Plain options contain only
`tls: false`. VLESS REALITY replaces the certificate ID with
`tls_mode: reality` and `reality_target_id`. Resource references are validated
but do not enter client output. TUIC and AnyTLS use the same certificate TLS
options without a `transport` field. TLS verification always remains enabled.
2022 keys use canonical standard Base64 of exactly 16 bytes for AES-128 and
32 bytes for AES-256/ChaCha20. Other passwords are 32–128 URL-safe ASCII
characters; SOCKS usernames are 8–64 URL-safe ASCII characters.

URI generation uses SIP002 Shadowsocks (Base64url userinfo for older AEAD,
percent-encoded plaintext userinfo for AEAD-2022), classic JSON/Base64 VMess,
and protocol-specific VLESS/Trojan/TUIC/AnyTLS parameters. SOCKS uses the pinned
Mihomo importer's Base64url authentication convention and an explicit `udp`
query. IPv6 authorities are bracketed; credentials, labels, and query values
are encoded individually. URI readers vary: Mihomo 1.19.32's raw importer
does not preserve HTTPUpgrade, Trojan WebSocket Host, or SOCKS UDP settings.
The generated Mihomo YAML is the lossless delivery format for these options;
producing a URI does not assert that every external importer understands it.

The pinned Mihomo YAML declares `udp: false` for VLESS/VMess H2. Dedicated-VPS
VLESS H2 UDP acceptance repeatedly timed out with Mihomo 1.19.32, while the
same server passed TCP and UDP through sing-box 1.14.3. An apparent later
success with `udp: false` was a DIRECT fallthrough false positive, not evidence
that the H2 path recovered. Both generated H2 node types share Mihomo's H2
transport, so neither advertises UDP support in this client format; the product
promises TCP only for H2 with this preset. This is an acceptance limit, not a
claim that H2 UDP can never work. The server retains H2 UDP capability, and
generic share URIs retain the H2 transport; an external URI importer does not
enforce this YAML boundary.

When any additional node declares `udp: false`, generated rules append
`MATCH,REJECT` after `MATCH,PROXY`. In rule mode, Mihomo skips a matching proxy
that does not advertise UDP and otherwise falls back to DIRECT. The final
reject rule prevents that unintended direct traffic. The original REALITY/HY2
input without extra UDP-limited nodes keeps its existing rule output. Explicit
`global` or `direct` mode overrides do not use this rule boundary; the UDP flag
does not itself guarantee rejection in every invocation path. The behavior is
visible in the pinned [UDP rule matcher](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/tunnel/tunnel.go)
and [selector capability check](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/adapter/outboundgroup/selector.go).

The [pinned H2 connection implementation](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/transport/vmess/h2.go)
initializes a request from either the first read or write without explicit
synchronization. This is a possible cause, not a demonstrated root cause.

The supported client transports were checked against the pinned
[VLESS implementation](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/adapter/outbound/vless.go),
[VMess implementation](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/adapter/outbound/vmess.go),
[Trojan implementation](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/adapter/outbound/trojan.go),
and [raw URI importer](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/common/convert/converter.go).
In particular, unknown Trojan network names fall through to TCP, and VLESS
WebSocket TLS does not apply REALITY; these combinations are rejected here.

The Python publication contract is documented in `proxyhub.publication`.
The publisher accepts only generated bundles and serves exact host/path pairs:
`/sub/<token>/mihomo.yaml` and `/sub/<token>/raw`. Filename, manual traffic,
expiry and refresh headers belong to each profile. `userinfo: {}` means unknown
and omits the traffic header. Upload, download and total must be supplied
together; expiry is independently optional. Explicit zero remains distinct
from unknown. Reset dates are management metadata and never become an expiry
header.

Tests run the real worker, a local HTTP publisher, and the cached pinned
Mihomo binary when available. Set `MIHOMO_BIN` to a pinned binary on other
platforms. A missing optional binary produces an explicit skipped config-check
test; config validation does not by itself prove a real proxied request or GUI
client metadata support.
