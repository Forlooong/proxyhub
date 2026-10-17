"""Pure website/Nginx rendering and explicit local certificate validation."""

from __future__ import annotations

import hmac
import html
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .proxy import validate_domain, validate_spec


def validate_certificate(
    cert_path: str | Path,
    key_path: str | Path,
    domains: Iterable[str],
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    """Check PEM key matching, DNS SANs, validity dates, and each hostname.

    OpenSSL is invoked without a shell, with bounded execution and captured
    output. Only public key bytes leave the key-reading subprocess. Encrypted
    private keys are rejected rather than prompting. Trust-chain validation
    is deliberately separate from these local import checks.
    """
    if isinstance(domains, (str, bytes)):
        raise ValueError("domains must be a collection of DNS names")
    try:
        names = list(dict.fromkeys(validate_domain(domain) for domain in domains))
    except TypeError:
        raise ValueError("domains must be a collection of DNS names") from None
    if not names:
        raise ValueError("at least one certificate domain is required")
    paths = [Path(cert_path), Path(key_path)]
    for label, path in zip(("certificate", "private key"), paths):
        try:
            valid = path.is_file() and 0 < path.stat().st_size <= 1024 * 1024
        except OSError:
            valid = False
        if not valid:
            raise ValueError(f"{label} must be a readable nonempty PEM file of at most 1 MiB")

    def invoke(arguments: list[str], *, data: bytes | None = None) -> bytes:
        try:
            completed = runner(
                ["openssl", *arguments], input=data, capture_output=True,
                check=False, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("OpenSSL certificate validation could not complete") from None
        if completed.returncode != 0:
            # Do not forward tool diagnostics: private paths and PEM fragments
            # must not be copied into CLI errors, journals, or audit evidence.
            raise ValueError("OpenSSL rejected the certificate, key, or requested hostname")
        return completed.stdout

    certificate_public_pem = invoke(["x509", "-in", str(paths[0]), "-pubkey", "-noout"])
    certificate_public_der = invoke(["pkey", "-pubin", "-outform", "DER"], data=certificate_public_pem)
    key_public_der = invoke([
        "pkey", "-in", str(paths[1]), "-passin", "pass:", "-pubout", "-outform", "DER",
    ])
    if not certificate_public_der or not hmac.compare_digest(certificate_public_der, key_public_der):
        raise ValueError("certificate and private key do not match")
    details = invoke([
        "x509", "-in", str(paths[0]), "-noout", "-dates", "-ext", "subjectAltName",
        "-fingerprint", "-sha256",
    ]).decode("ascii", errors="replace").replace("\r\n", "\n")
    if not re.search(r"(?:^|[\s,])DNS:[^\s,]+", details):
        raise ValueError("certificate must include DNS subject alternative names")
    dates: dict[str, datetime] = {}
    for field in ("notBefore", "notAfter"):
        match = re.search(rf"^{field}=(.+)$", details, re.MULTILINE)
        if match is None:
            raise ValueError("certificate validity dates could not be read")
        try:
            dates[field] = datetime.strptime(match[1].strip(), "%b %d %H:%M:%S %Y GMT").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise ValueError("certificate validity dates could not be read") from None
    now = datetime.now(timezone.utc)
    if not dates["notBefore"] <= now < dates["notAfter"]:
        raise ValueError("certificate is expired or not yet valid")
    for domain in names:
        result = invoke(["x509", "-in", str(paths[0]), "-noout", "-checkhost", domain])
        # OpenSSL 3.0 returns exit 0 even for a hostname mismatch. Require its
        # positive result, not merely process success (newer versions differ).
        if result.decode('ascii', errors='replace').strip() != f'Hostname {domain} does match certificate':
            raise ValueError('Certificate hostname does not match the requested domain')
    fingerprint = re.search(r"^sha256 Fingerprint=((?:[A-Fa-f0-9]{2}:){31}[A-Fa-f0-9]{2})$", details, re.MULTILINE | re.IGNORECASE)
    if fingerprint is None:
        raise ValueError("certificate SHA256 fingerprint could not be read")
    return {
        "domains": names,
        "not_before": dates["notBefore"].isoformat(),
        "not_after": dates["notAfter"].isoformat(),
        "sha256": fingerprint[1].replace(":", "").lower(),
    }


def render_nginx(spec: Mapping[str, Any], revision_dir: str, web: dict | None = None,
                 reality: dict | None = None) -> str:
    """Render a standalone config; revision_dir must be an immutable managed path.

    Nginx must provide statically loaded stream, ssl_preread, HTTP SSL, and HTTP
    request-limit modules. Installers must verify their actual binary/modules.
    No package-global default config or dynamic-module wildcard is included.
    """
    from .websites import get_certificate, normalize_web, site_directory

    value = validate_spec(spec)
    if not isinstance(revision_dir, str) or not re.fullmatch(r"/var/lib/proxyhub/revisions/[0-9a-f]{8,64}", revision_dir):
        raise ValueError("revision_dir must be /var/lib/proxyhub/revisions/<8-64 lowercase hex digits>")
    site, subscription = value["site_domain"], value["subscription_domain"]
    resources = normalize_web({'server': value, **({'web': web} if web is not None else {})})
    active_sites = [website for website in resources['sites'] if website['enabled']]
    active_domains = {domain for website in active_sites for domain in website['domains']} | {subscription}
    challenge_domains = list(dict.fromkeys([
        *(domain for website in resources['sites'] for domain in website['domains']),
        subscription,
        *(domain for certificate in resources['certificates'] for domain in certificate['domains']),
    ]))
    https_suffix = "" if value["public_port"] == 443 else f":{value['public_port']}"
    def publication_location(selector):
        return f"""        {selector} {{
            access_log off;
            error_log /dev/null crit;
            limit_except GET {{ deny all; }}
            limit_req zone=proxyhub_subscription burst=20 nodelay;
            limit_req_status 429;
            proxy_pass http://127.0.0.1:{value['publisher_port']};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Forwarded-For "";
            proxy_set_header X-Real-IP "";
            proxy_set_header Connection "";
            proxy_set_header Content-Length "";
            proxy_pass_request_body off;
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_max_temp_file_size 0;
            proxy_connect_timeout 3s;
            proxy_read_timeout 10s;
            proxy_send_timeout 10s;
        }}"""
    # Route only credential-shaped paths on existing managed website TLS
    # listeners. No additional listener, certificate or proxy process is made.
    website_publication = publication_location(
        'location ~ "^/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]{32,128}/"')
    redirects = []
    for domain in challenge_domains:
        response = (f'return 308 https://{domain}{https_suffix}$request_uri;'
                    if domain in active_domains else 'return 404;')
        redirects.append(f"""    server {{
        listen {value['http_port']};
        listen [::]:{value['http_port']};
        server_name {domain};
        location ^~ /.well-known/acme-challenge/ {{
            root /var/lib/proxyhub/acme;
            default_type text/plain;
            limit_except GET {{ deny all; }}
            try_files $uri =404;
        }}
        location / {{ {response} }}
    }}""")
    from .reality import public_resources
    entries = public_resources({'server':value,'web':resources,
                                **({'reality':reality} if reality is not None else {})})['endpoints']
    reality_domains = {entry['sni'] for entry in entries}
    routes = [f"        {entry['sni']} 127.0.0.1:{entry['listen_port']};" for entry in entries]
    routes.append(f"        {subscription} 127.0.0.1:{value['subscription_tls_port']};")
    website_servers = []
    for website in active_sites:
        certificate = get_certificate(resources, website['certificate_id'])
        domains = website['domains']
        routes.extend(f"        {domain} 127.0.0.1:{website['tls_port']};"
                      for domain in domains if domain not in reality_domains)
        host_check = (f"if ($host != {domains[0]}) {{ return 444; }}" if len(domains) == 1 else
                      'if ($host !~ ^(?:' + '|'.join(re.escape(domain) for domain in domains) + ')$) { return 444; }')
        website_servers.append(f"""    server {{
        listen 127.0.0.1:{website['tls_port']} ssl;
        server_name {' '.join(domains)};
        {host_check}
        ssl_certificate {certificate['certificate_path']};
        ssl_certificate_key {certificate['key_path']};
        root {revision_dir}/{site_directory(website['id'])};
        index index.html;
        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy no-referrer always;
        add_header Content-Security-Policy "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'" always;
        location = /__proxyhub_publication__ {{ return 404; }}
{website_publication}
        location / {{
            limit_except GET {{ deny all; }}
            try_files $uri $uri/ =404;
        }}
    }}""")
    subscription_certificate = get_certificate(resources, resources['subscription_certificate_id'])
    return f"""# ProxyHub managed configuration; requires stream + ssl_preread + HTTP SSL.
user proxyhub-web;
worker_processes 1;
pid /run/proxyhub/nginx.pid;
error_log stderr warn;

events {{ worker_connections 1024; }}

stream {{
    map $ssl_preread_server_name $proxyhub_tls_upstream {{
        default 127.0.0.1:1;
{chr(10).join(routes)}
    }}
    server {{
        listen {value['public_port']};
        listen [::]:{value['public_port']};
        ssl_preread on;
        proxy_pass $proxyhub_tls_upstream;
        proxy_connect_timeout 5s;
        proxy_timeout 1h;
        access_log off;
    }}
}}

http {{
    default_type application/octet-stream;
    types {{
        text/html html htm; text/css css; application/javascript js;
        application/json json; text/plain txt; image/svg+xml svg;
        image/png png; image/jpeg jpg jpeg; image/gif gif; image/webp webp;
        image/x-icon ico; font/woff woff; font/woff2 woff2; font/ttf ttf; font/otf otf;
    }}
    charset utf-8;
    server_tokens off;
    access_log off;
    client_max_body_size 1k;
    client_body_timeout 5s;
    client_header_timeout 5s;
    keepalive_timeout 10s;
    send_timeout 10s;
    client_body_temp_path /var/lib/proxyhub/nginx/client_temp;
    proxy_temp_path /var/lib/proxyhub/nginx/proxy_temp;
    fastcgi_temp_path /var/lib/proxyhub/nginx/fastcgi_temp;
    uwsgi_temp_path /var/lib/proxyhub/nginx/uwsgi_temp;
    scgi_temp_path /var/lib/proxyhub/nginx/scgi_temp;
    # Stream terminates at loopback: this is an explicit aggregate rate limit.
    limit_req_zone $server_name zone=proxyhub_subscription:1m rate=10r/s;
    ssl_protocols TLSv1.3;
    ssl_ecdh_curve X25519:prime256v1;
    ssl_session_cache shared:ProxyHubTLS:1m;
    ssl_session_timeout 10m;
    ssl_session_tickets off;

    server {{
        listen {value['http_port']} default_server;
        listen [::]:{value['http_port']} default_server;
        server_name _;
        location ^~ /.well-known/acme-challenge/ {{
            root /var/lib/proxyhub/acme;
            default_type text/plain;
            limit_except GET {{ deny all; }}
            try_files $uri =404;
        }}
        location / {{ return 444; }}
    }}
{chr(10).join(redirects)}

{chr(10).join(website_servers)}

    server {{
        listen 127.0.0.1:{value['subscription_tls_port']} ssl;
        server_name {subscription};
        if ($host != {subscription}) {{ return 444; }}
        ssl_certificate {subscription_certificate['certificate_path']};
        ssl_certificate_key {subscription_certificate['key_path']};
        access_log off;
        error_log /dev/null crit;
        location = /__proxyhub_publication__ {{ return 404; }}
{publication_location('location /')}
    }}
}}
"""


def render_site(spec: Mapping[str, Any]) -> str:
    """Return a self-contained, escaped static landing page without admin data."""
    value = validate_spec(spec)
    title = html.escape(value["site_title"], quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: #173b3a; background: #f4f3ed; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 32px 20px; }}
    main {{ width: min(760px, 100%); border-top: 4px solid #27766a; padding: 52px 0 24px; }}
    .status {{ display: inline-flex; align-items: center; gap: 10px; padding: 9px 14px; border: 1px solid #c7d8cf; border-radius: 24px; color: #286458; font-size: 13px; letter-spacing: .12em; }}
    .status::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: #347d59; }}
    h1 {{ margin: 42px 0 16px; font-size: clamp(32px, 7vw, 64px); font-weight: 600; letter-spacing: -.04em; overflow-wrap: anywhere; }}
    p {{ margin: 0; color: #637674; font-size: 17px; line-height: 1.8; }}
    footer {{ margin-top: 80px; padding-top: 20px; border-top: 1px solid #d8dfd7; display: flex; justify-content: space-between; gap: 16px; color: #75817b; font-size: 12px; }}
    @media (max-width: 480px) {{ main {{ padding-top: 32px; }} footer {{ margin-top: 56px; }} }}
  </style>
</head>
<body>
  <!-- ProxyHub managed website -->
  <main>
    <div class="status">服务在线</div>
    <h1>{title}</h1>
    <p>欢迎来到这里。愿每一次连接，都简单而顺畅。</p>
    <footer><span>保持连接，轻松浏览。</span><span>ONLINE</span></footer>
  </main>
</body>
</html>
"""
