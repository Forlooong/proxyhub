# Phase 0 and Minimal Loop Implementation Plan

> **For agentic workers:** Use subagent-driven-development for isolated components and independent spec/code review. Execute the already authorized scope without another approval handoff.

**Goal:** Establish an independent ProxyHub repository, verify pinned upstream capabilities, and demonstrate append-only SSH authorization, a managed HTTPS website sharing its public entrance with REALITY, and a usable Mihomo subscription on the designated test VPS.

**Architecture:** A namespaced Python operation layer prepares immutable revisions and explicit plans; a TypeScript worker produces client configuration. Phase 1 CLI exercises the same operations later exposed by the local manager socket/API. A separate read-only publisher serves complete body/header bundles through Nginx. Existing installations and commands are discovered and left owned by their original manager.

**Tech Stack:** Python 3.11+, Node 24/TypeScript, SQLite, Nginx stream ssl_preread, pinned sing-box and Mihomo, OpenSSH, ACME HTTP-01.

## Scope and file ownership

The accepted complete product design remains in `docs/superpowers/specs/2026-10-14-proxyhub-design.md`. This plan delivers stages 0–1 only; the full visual panel, HY2 lifecycle, rule CI, multiple accounts and automatic metering are later stages. Core integration tests must use actual binaries and real network requests, not mocked success. Public evidence excludes the test host, account secrets, UUIDs, private keys and full subscription URLs.

| Files | Responsibility |
| --- | --- |
| `evidence/sources.lock.json`, `docs/phase0-sources.md` | Source commits, licenses, selected capabilities |
| `evidence/runtime-assets.json`, `scripts/verify_runtime.py`, `evidence/runtime-verification.json` | Download hashes, binary versions and capability probes |
| `src/proxyhub/host/ssh.py`, `tests/test_ssh.py` | Parse options and key identity; effective files; plan and atomic append |
| `src/proxyhub/core.py`, `state.py`, `cli.py`, `__main__.py` | Named operations, state revision, offline help and CLI |
| `src/proxyhub/site.py`, `proxy.py`, `deployment.py` | Managed static content, certificates, locked REALITY config and namespaced activation |
| `packages/converter/src/worker.ts`, `tests/test_converter.py` | One client configuration composer, protected fields, VLESS URI and YAML |
| `src/proxyhub/publication.py`, `publisher.py` | Profile metadata, immutable bundle and loopback GET/HEAD runtime |
| `scripts/accept_live.py`, `docs/phase1-acceptance.md` | Real website, proxy, subscription and SSH evidence |

## Task 1: Stage 0 source and binary gate

- [x] Resolve every selected upstream ref to a complete commit and record actual LICENSE content, not the repository API's inferred license.
- [x] Obtain official release asset URLs and digests; download and compare SHA256 before execution. Failed downloads remain failures, not evidence of unsupported capability.
- [x] Run locked binary version and configuration checks; verify REALITY fields, default statistics unavailability, Nginx stream module and actual client configuration acceptance.
- [x] Record separate source_verified, hash_verified, executable_verified and live_verified statuses. Only the supported architecture can receive an executable_verified claim.

Commands:

```sh
python scripts/verify_runtime.py --help
python scripts/verify_runtime.py
python -m json.tool evidence/sources.lock.json >/dev/null
python -m json.tool evidence/runtime-verification.json >/dev/null
```

Expected: per-artifact hashes match; unsupported/unchecked architectures are explicitly marked; V2Ray API absence is recorded as an expected capability result. Ubuntu 22.04 uses an isolated Python >=3.11 runtime or stays a documented pending bootstrap target; it is not silently run on its Python 3.10 system interpreter.

## Task 2: SSH key append with an independent session

- [x] Build key parser and plan functions first, then exercise their failure cases with real OpenSSH-generated keys.
- [x] Preserve exact existing bytes, treat comment-only differences as duplicates, and reject equal key blobs with differing restrictions. Reject private key input, invalid base64, symlink targets and concurrent file changes.
- [x] Read `sshd -T -C` for the chosen account/context. Resolve all effective authorization paths; do not create a fallback path if the effective configuration is unsupported.
- [x] Apply a stored plan after rechecking file hashes; write a uniquely named restricted backup and atomically replace the selected file with correct owner/mode.
- [x] On the dedicated test VPS, create a task-specific local key, append its public half through ProxyHub, and prove a second independent key-authenticated session. Keep password and port policy unchanged in this phase; do not claim the later hardening workflow is implemented.

Behavior contract:

```python
from proxyhub.host.ssh import parse_authorized_key

def same_identity_with_new_comment(original, renamed):
    assert parse_authorized_key(original).identity == parse_authorized_key(renamed).identity
```

Run `python -m pytest tests/test_ssh.py -q`; require duplicate, restrictions, malformed key, effective-path, unchanged old bytes and concurrent-change cases to pass. Live evidence records the fingerprint and success booleans, never the key's private material.

## Task 3: Website and REALITY operation model

- [x] Add a validated deployment specification separating public address, site SNI, subscription SNI, internal ports and credentials. Fixed internal loopback targets cannot point back at public TCP 443.
- [x] Render namespaced Nginx configuration, immutable static website content and sing-box configuration. Inspect ports and service ownership before activation. Reuse no existing global binary or configuration filename.
- [x] Obtain a certificate with HTTP-01 only after DNS and TCP 80 are verified. Imported certificates require key/SAN/complete-PEM validation; deployment TLS health verifies trust. Certificate files and UUID/private keys never appear in public plan output.
- [x] Validate Nginx and sing-box candidates, start namespaced services, test website TLS, then run a real client request through REALITY. Journal candidate activation and retain a known-good prior revision if a check fails.

The config invariant is:

```python
def assert_internal_handshake(config):
    inbound = config['inbounds'][0]
    assert inbound['tls']['reality']['handshake']['server'] == '127.0.0.1'
    assert inbound['listen'] == '127.0.0.1'
    assert 'certificate_path' not in inbound['tls']
```

Run `python -m pytest tests/test_deployment.py -q`. The real acceptance run must separately demonstrate ordinary HTTPS fallback and an authenticated REALITY request; config validation alone does not pass the gate.

## Task 4: One converter and atomic subscription runtime

- [x] Implement a strict TypeScript composer for the minimal REALITY node, one explicit select group, DNS bootstrap and final MATCH policy. Fail on unsupported fields rather than discarding them.
- [x] Support persistent permitted client overrides; reject attempts to overwrite generated nodes/groups/rules. Generate URI/raw output from the same node snapshot.
- [x] Bind YAML/raw, response metadata and exact host/path routes into one immutable profile bundle. Encode filename* correctly, distinguish expiry and reset metadata, and keep manual traffic labelled as such.
- [x] Publisher binds loopback, accepts GET/HEAD only, performs no outbound fetch, and retains the complete old bundle on reload failure. Each request sees one generation. Tokens and body credentials are excluded from normal logs.
- [x] Download the subscription through its public HTTPS endpoint, parse it with the locked client core, and perform a real request with the downloaded config.

Commands:

```sh
npm ci
npm run build
python -m pytest tests/test_converter.py tests/test_publication.py -q
```

Expected: protected-field conflicts fail; metadata contains reset information only in its management fields; downloaded config is accepted by locked Mihomo and carries an actual proxied request.

## Task 5: CLI, installation and reviewable delivery

- [x] Add offline/no-state help, module names, plan/execute commands, bounded JSON output and secrets redaction. Namespaced install creates only `proxyhub` and `proxyhub-*` resources.
- [x] Run Python/TypeScript tests, real binary config checks and live VPS acceptance. Keep public sanitized evidence separate from ignored private deployment state.
- [x] Conduct independent spec review, resolve findings, then independent code-quality review and targeted retests.
- [x] Create the independent public GitHub repository, publish only reviewed source and sanitized evidence, and record the commit and exact remaining stage gates. Existing projects, commands and unrelated services stay unchanged; the test host required a separate public-key-authentication enable operation, while password, root-login and port policies were retained.

Run `python -m proxyhub help` with no state directory and no network. Run `git diff --check`, inspect the exact staged file list and scan for deployment secrets before committing or publishing.

Implementation status: stages 0–1 verified on the dedicated test environment; publishing is finalized with the initial implementation commit. See docs/phase1-acceptance.md for explicit remaining gates.
