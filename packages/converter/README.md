# Minimal subscription converter

This independently implemented worker supports one VLESS REALITY TCP/Vision
node. It does not import external subscriptions, fetch rules, or provide a
second configuration generator.

Build with Node 24:

```sh
npm ci
npm run build
node packages/converter/dist/worker.js < input.json > output.json
```

The worker reads one UTF-8 JSON document, at most 64 KiB, from stdin. Successful
stdout is one JSON document containing `yaml`, `raw`, and `uri`. `raw` is standard
Base64 of the URI plus a newline. Invalid, unknown, duplicate, or unsupported
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
