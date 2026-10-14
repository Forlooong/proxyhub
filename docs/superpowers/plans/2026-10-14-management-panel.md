# Management Panel Implementation Plan

> **For agentic workers:** Use subagent-driven-development for isolated UI, transport/security and operation changes; independently review and exercise the integrated result. The user has approved the revised design and authorized implementation and dedicated-VPS acceptance.

**Goal:** Deliver a real management panel for the existing single-node SSH, website/certificate, REALITY and subscription operations, preserving old commands and the running public services.

**Architecture:** Static React/TypeScript/Vite assets are served by a low-privilege FastAPI gateway on loopback port 18765. The authenticated gateway calls a root manager through a peer-checked Unix socket. The manager uses the same validated Python operations, plans, revision checks and recovery as the CLI. Slow operations become persisted jobs; no browser-controlled shell, arbitrary service name or root filesystem path is accepted.

**Tech Stack:** Existing Python 3.11+, SQLite and Node 24; pinned React/Vite/FastAPI/Uvicorn dependencies. No VPS frontend compilation.

## Visual plan and critique

Use the approved palette: mist `#F5F7FA`, paper `#FFFFFF`, ink `#182536`, slate `#536477`, action blue `#215AC8`, warning `#B86A12`. Self-host IBM Plex Sans for Latin, system Chinese sans-serif, Plex Mono for command/code values. Text remains left aligned, controls use a consistent 14/16px scale, page headings 28–32px. Deep mode preserves semantic contrast.

```
navigation | page heading / context                     | change drawer
           | interactive website–certificate–node–link | before / after
           | resource details or editable form          | effect / apply
           | latest real operation results              | cancel
```

The memorable element is a useful topology with focusable resource buttons. Avoid a repeated grid of numeric cards, fake traffic, decorative animation, and portfolio hero sections: this is an operations workspace. Motion only answers selection or state changes (120–180ms); reduced-motion disables it. Mobile uses a compact navigation selector and a full-width drawer. Follow Brittany Chiang's persistent orientation and Lee Robinson's economical grouping, without copying their code or branding. References: developer-portfolios, brittanychiang.com and leerob.com.

## Shared API contract

Gateway: `GET /api/session` -> `{authenticated, csrf?}`; `POST /api/login {credential}` -> `{authenticated:true, csrf}` and HttpOnly SameSite=Strict session cookie; `POST /api/logout`. Mutating requests require exact Origin and `X-CSRF-Token` except login, which still requires same Origin. No CORS. All API responses are no-store; CSP/frame protection apply. Credential never enters URL or localStorage.

Authenticated operations: `POST /api/operation {operation, payload}`. Response is `{ok:true, result}` or `{ok:false,error:{code,message}}`. Slow operations return `{job_id}`; poll `job.get {job_id}`. Read failures remain explicit; no generated/demo data in the production bundle.

Operations:

| operation | payload | result |
| --- | --- | --- |
| `overview` | `{}` | `{revision, server, website, certificate, proxy, subscription, services, capabilities, recent_jobs}`; secrets omitted |
| `jobs.list` | `{}` | `{jobs:[{id,operation,status,created_at,finished_at,result?,error?}]}` |
| `job.get` | `{job_id}` | same single job; status queued/running/succeeded/failed/interrupted |
| `ssh.inspect` | `{account,context:{addr,host,laddr,lport}}` | `{account,paths:[{id,label}],public_key_authentication}` |
| `plan.create` | `{kind,base_revision,values}` | `{job_id}`, completed job result is the preview below |
| `plan.apply` | `{plan_id}` | `{job_id}`, completed job includes applied/noop and verification status |
| `subscription.reveal` | `{format:'links'|'yaml'}` | deliberate secret reveal `{links:{mihomo,raw}}` or `{yaml}`; no private server key |
| `deployment.recover` | `{}` | `{job_id}` |

Preview: `{plan_id,kind,base_revision,summary,changes:[{label,before,after}],impact:[string],warnings:[string]}`. Values, previews and job results never expose a private key, node UUID, short ID or URL token. Account paths are displayed but selected by opaque ID derived from current effective sshd settings.

Plan kinds and exact values:

- `ssh.append`: `{account,public_key,path_id,context:{addr,host,laddr,lport}}`; retain all original bytes, show fingerprint and duplicate/conflict outcome, always mark post-write login unverified.
- `website.update`: `{site_title}`; retain domain, target and credential relationships.
- `proxy.update`: `{node_name,public_address}`; show affected subscription and reload impact. Current target is the managed local website. Unsupported HY2/external targets are described as later capabilities, never presented as working choices.
- `subscription.update`: `{name,update_interval_hours,userinfo,reset_day,reset_at,overrides}`; optional reset values use null, userinfo may be `{}`. Overrides is a restricted JSON object accepted by the sole TS generator; the final YAML can be explicitly revealed after generation. Preserve generated fields; show allowed-field errors. Publish-only updates must keep proxy and Nginx PIDs unchanged and verify the publisher loaded the new generation.
- `certificate.renew`: `{}`; explicit preview before ACME work, retain external-import ownership rules.
- `certificate.import`: `{certificate_pem,private_key_pem}`; import to private staging, validate, bind to existing consumers; key field cleared after submit and never returned.

Overview fields: server `{hostname,os,architecture,memory_total_bytes,accounts:[string],ssh_context:{addr,host,laddr,lport}}`; website `{id,domain,title,url}`; certificate `{id,domains,not_after,sha256,automatic_renewal,consumers:[string]}`; proxy `{id,name,protocol,public_address,port,sni,target,verification}`; subscription `{id,name,domain,update_interval_hours,userinfo,reset_day,reset_at,overrides,traffic_source}`; services `[{name,status}]`; capabilities `[{name,available,reason}]`. Missing active deployment yields explicit setup-required error, not fictitious rows.

## Tasks and acceptance

- [x] Implement and test a publish-only activation path in `deployment.py`: strict immutable candidate/hash/revision checks, publisher acknowledgement, rollback and crash recovery, no proxy/Nginx restart.
- [x] Implement `management.py` typed operations, `manager.py` Unix RPC/jobs and `gateway.py` login/session/CSRF/host controls. Tests must reject unknown operation/fields, paths, origins, missing auth/CSRF, stale plans and secret leakage; job interruption is visible after manager restart.
- [x] Add `panel_install.py` and `proxyhub panel` CLI commands. Installer creates only namespaced users/units/socket/credentials; private gateway cannot read node keys/state. CLI can send the same typed operations.
- [x] Build `packages/panel` against the shared API. Overview topology, SSH form, website/certificate form, REALITY form, subscription editor/explicit reveal, jobs, help, theme, command menu, copy CLI and accessible preview/apply drawer all use real endpoints. No placeholder action controls.
- [x] Build locally, exercise gateway/manager tests and existing tests, install pinned wheels/assets on the authorized VPS, keep panel loopback-only and open a verified SSH tunnel.
- [x] In a real browser test login, navigation, light/dark, mobile, preview/cancel/apply, subscription edit, public-key duplicate plan, error handling and session logout. Verify public website/REALITY/subscription still work; publisher-only updates preserve proxy PID.
- [x] Run independent code/security review, fix concrete findings, save sanitized evidence and private screenshots/access instructions, publish the reviewed commit to the independent repository.

This increment makes the existing single-node functions usable in the panel. Multiple sites, website ZIP/version lifecycle, SNI migration, HY2, rule pipelines and automatic traffic accounting retain their separately documented stage gates; the panel must expose these limits honestly.

Verified on the dedicated Debian 12 amd64 VPS and real browser. See docs/phase2-panel-acceptance.md for results, repaired integration failures, and remaining stage gates.
