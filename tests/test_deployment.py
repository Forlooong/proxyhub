"""Deployment renderer tests, including locally available pinned binaries."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from proxyhub.proxy import render_sing_box, validate_spec
from proxyhub.site import render_nginx, render_site, validate_certificate


ROOT = Path(__file__).resolve().parents[1]
REVISION = "/var/lib/proxyhub/revisions/0123456789abcdef"


def deployment_spec(**updates):
    # Public RFC 7748 test vector; these are not deployment credentials.
    private = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    public = bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    value = {
        "site_domain": "site.example.test",
        "subscription_domain": "sub.example.test",
        "public_address": "203.0.113.10",
        "uuid": "a875d3ac-980f-4a0c-929e-78cc678429f1",
        "private_key": base64.urlsafe_b64encode(private).decode().rstrip("="),
        "public_key": base64.urlsafe_b64encode(public).decode().rstrip("="),
        "short_id": "0123456789abcdef",
        "certificate_path": "/var/lib/proxyhub/certificates/0123456789abcdef/fullchain.pem",
        "key_path": "/var/lib/proxyhub/certificates/0123456789abcdef/privkey.pem",
    }
    value.update(updates)
    return value


class SpecValidationTests(unittest.TestCase):
    def test_defaults_normalization_and_no_mutation(self):
        original = deployment_spec(site_domain="SITE.EXAMPLE.TEST", short_id="ABCD")
        original["private_key"] += "="
        result = validate_spec(original)
        self.assertEqual(result["site_domain"], "site.example.test")
        self.assertEqual(result["short_id"], "abcd")
        self.assertEqual(result["public_port"], 443)
        self.assertEqual(result["http_port"], 80)
        self.assertEqual(result["site_tls_port"], 8443)
        self.assertEqual(len(result["private_key"]), 43)
        self.assertEqual(original["site_domain"], "SITE.EXAMPLE.TEST")
        self.assertNotIn("public_port", original)

    def test_domain_injection_local_names_and_wildcards_are_rejected(self):
        for domain in (
            "localhost", "a.localhost", "a.local", "*.example.test", "example.test;",
            "example.test\n}", "example.test other.test", "example.test/", "example.test.",
            "a..test", "-bad.test", "bad-.test", "_srv.example.test", "例子.test",
            "127.0.0.1", "https://example.test", "a" * 64 + ".test", ".example.test",
        ):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                validate_spec(deployment_spec(site_domain=domain))
        with self.assertRaises(ValueError):
            validate_spec(deployment_spec(subscription_domain="SITE.EXAMPLE.TEST"))

    def test_public_address_accepts_ip_or_strict_hostname(self):
        self.assertEqual(validate_spec(deployment_spec(public_address="2001:0db8::01"))["public_address"], "2001:db8::1")
        self.assertEqual(validate_spec(deployment_spec(public_address="NODE.EXAMPLE.TEST"))["public_address"], "node.example.test")
        for address in ("fe80::1%eth0", "[2001:db8::1]", "host:443", "127.0.0.1;", "", None):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_spec(deployment_spec(public_address=address))

    def test_ports_must_be_distinct_bounded_integers(self):
        fields = ("public_port", "http_port", "reality_port", "site_tls_port", "subscription_tls_port", "publisher_port")
        for field in fields:
            for invalid in (0, 65536, -1, True, "443", 443.0):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    validate_spec(deployment_spec(**{field: invalid}))
        for field in fields[1:]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_spec(deployment_spec(**{field: 443}))

    def test_uuid_short_id_and_key_encoding(self):
        canonical = "a875d3ac-980f-4a0c-929e-78cc678429f1"
        self.assertEqual(validate_spec(deployment_spec(uuid=canonical.upper()))["uuid"], canonical)
        for invalid in (canonical.replace("-", ""), "{" + canonical + "}", "not-a-uuid", None):
            with self.subTest(uuid=invalid), self.assertRaises(ValueError):
                validate_spec(deployment_spec(uuid=invalid))
        for invalid in ("", "0", "abc", "00" * 9, "gh", ["abcd"]):
            with self.subTest(short_id=invalid), self.assertRaises(ValueError):
                validate_spec(deployment_spec(short_id=invalid))
        for field in ("private_key", "public_key"):
            for invalid in ("", "A" * 42, "A" * 44, "/" * 43, "A" * 42 + "B", "A" * 43 + "\n"):
                with self.subTest(field=field, key=invalid), self.assertRaises(ValueError):
                    validate_spec(deployment_spec(**{field: invalid}))

    def test_reject_unknown_missing_and_invalid_text_fields(self):
        with self.assertRaises(ValueError):
            validate_spec(deployment_spec(subcription_domain="typo.example.test"))
        value = deployment_spec()
        del value["private_key"]
        with self.assertRaises(ValueError):
            validate_spec(value)
        for field in ("node_name", "site_title"):
            for invalid in ("", " ", "hello\nworld", "hello\x00", "a" * 121, None):
                with self.subTest(field=field, text=invalid), self.assertRaises(ValueError):
                    validate_spec(deployment_spec(**{field: invalid}))
        with self.assertRaises(ValueError):
            validate_spec([])

    def test_managed_certificate_paths_reject_escapes_and_directive_injection(self):
        for path in (
            "/etc/ssl/cert.pem", "/var/lib/proxyhub/certificates/../secret.pem",
            "/var/lib/proxyhub/certificates//a.pem", "/var/lib/proxyhub/certificates/./a.pem",
            "/var/lib/proxyhub/certificates/", "/var/lib/proxyhub/certificates/a.pem;root /;",
            "/var/lib/proxyhub/certificates/$foo.pem", "/var/lib/proxyhub/certificates/a\nb.pem",
            "/var/lib/proxyhub/certificates/a\\b.pem",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_spec(deployment_spec(certificate_path=path))
        with self.assertRaises(ValueError):
            validate_spec(deployment_spec(key_path=deployment_spec()["certificate_path"]))
        with self.assertRaises(ValueError):
            validate_spec(deployment_spec(key_path="/var/lib/proxyhub/certificates/abcdef0123456789/privkey.pem"))


class DeploymentRenderingTests(unittest.TestCase):
    def test_sni_route_graph_has_no_public_handshake_loop(self):
        config = render_sing_box(deployment_spec())
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["listen_port"], 2443)
        self.assertEqual(inbound["users"][0]["flow"], "xtls-rprx-vision")
        self.assertEqual(inbound["tls"]["server_name"], "site.example.test")
        self.assertEqual(inbound["tls"]["reality"]["handshake"], {"server": "127.0.0.1", "server_port": 8443})
        self.assertNotIn("certificate_path", inbound["tls"])
        self.assertNotIn("key_path", inbound["tls"])
        self.assertNotIn("experimental", config)
        self.assertEqual(config["outbounds"], [{"type": "direct", "tag": "direct"}])
        nginx = render_nginx(deployment_spec(), REVISION)
        self.assertIn("site.example.test 127.0.0.1:2443;", nginx)
        self.assertIn("sub.example.test 127.0.0.1:8444;", nginx)
        self.assertIn("default 127.0.0.1:1;", nginx)
        self.assertIn("listen 127.0.0.1:8443 ssl;", nginx)
        self.assertIn("listen 127.0.0.1:8444 ssl;", nginx)
        self.assertIn("ssl_preread on;", nginx)
        self.assertIn("ssl_protocols TLSv1.3;", nginx)
        self.assertIn("ssl_ecdh_curve X25519:prime256v1;", nginx)
        self.assertIn("ssl_certificate " + deployment_spec()["certificate_path"] + ";", nginx)
        self.assertNotIn("quic", nginx.lower())
        self.assertNotIn("http3", nginx.lower())
        self.assertNotIn("127.0.0.1:443", nginx)

    def test_publisher_and_http_boundary(self):
        nginx = render_nginx(deployment_spec(public_port=10443), REVISION)
        self.assertIn("proxy_set_header Host $host;", nginx)
        self.assertIn('location ~ "^/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]{32,128}/"', nginx)
        self.assertEqual(nginx.count('location = /__proxyhub_publication__ { return 404; }'), 2)
        self.assertIn("proxy_pass http://127.0.0.1:18890;", nginx)
        self.assertIn("limit_req zone=proxyhub_subscription", nginx)
        self.assertIn("client_max_body_size 1k;", nginx)
        self.assertIn("proxy_pass_request_body off;", nginx)
        self.assertIn("proxy_buffering off;", nginx)
        self.assertIn("error_log /dev/null crit;", nginx)
        self.assertIn("proxy_read_timeout 10s;", nginx)
        self.assertIn("root /var/lib/proxyhub/acme;", nginx)
        self.assertIn("return 308 https://site.example.test:10443$request_uri;", nginx)
        self.assertIn("return 308 https://sub.example.test:10443$request_uri;", nginx)
        self.assertNotIn("https://$host", nginx)
        self.assertIn("user proxyhub-web;", nginx)
        self.assertIn("pid /run/proxyhub/nginx.pid;", nginx)
        self.assertIn("root " + REVISION + "/site;", nginx)

    def test_revision_paths_are_exact_managed_hex_paths(self):
        for path in ("/tmp/test", REVISION + "/../escape", REVISION + ";", REVISION + "/", "/var/lib/proxyhub/revisions/$revision", "/var/lib/proxyhub/revisions/abc", "/var/lib/proxyhub/revisions/ABCDEF12"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                render_nginx(deployment_spec(), path)

    def test_static_site_escapes_titles_and_contains_no_admin_data(self):
        spec = deployment_spec(site_title='<script>alert("x")</script> & 欢迎')
        page = render_site(spec)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; 欢迎", page)
        self.assertNotIn("<script>", page)
        self.assertIn("服务在线", page)
        self.assertIn("ProxyHub managed website", page)
        for secret in (spec["uuid"], spec["private_key"], spec["public_key"], spec["short_id"], spec["subscription_domain"]):
            self.assertNotIn(secret, page)
        self.assertNotIn("https://", page)
        self.assertNotIn("http://", page)

    def test_real_sing_box_configuration_check_when_binary_available(self):
        native = ROOT / ".cache" / "runtime" / "windows-amd64" / "sing-box.exe"
        binary = os.environ.get("PROXYHUB_SING_BOX") or (str(native) if sys.platform == "win32" and native.is_file() else shutil.which("sing-box"))
        if not binary:
            self.skipTest("sing-box is unavailable; set PROXYHUB_SING_BOX to the pinned executable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sing-box.json"
            path.write_text(json.dumps(render_sing_box(deployment_spec())), encoding="utf-8")
            completed = subprocess.run([binary, "check", "-c", str(path)], capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)


class CertificateValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cert = Path(self.directory.name) / "cert.pem"
        self.key = Path(self.directory.name) / "key.pem"
        self.cert.write_text("certificate fixture", encoding="ascii")
        self.key.write_text("private fixture", encoding="ascii")
        self.names = ["site.example.test", "sub.example.test"]

    def runner(self, *, mismatch=False, missing_san=False, expired=False, future=False, reject_host=False, zero_exit_mismatch=False):
        now = datetime.now(timezone.utc)
        before = now + timedelta(days=1) if future else now - timedelta(days=1)
        after = now - timedelta(seconds=1) if expired else now + timedelta(days=2)
        details = f"notBefore={before:%b %d %H:%M:%S %Y GMT}\nnotAfter={after:%b %d %H:%M:%S %Y GMT}\n"
        if not missing_san:
            details += "X509v3 Subject Alternative Name:\n    DNS:site.example.test, DNS:sub.example.test\n"
        details += "sha256 Fingerprint=" + ":".join(["AB"] * 32) + "\n"
        details = details.replace("\n", "\r\n")

        def run(arguments, **kwargs):
            self.assertEqual(kwargs["timeout"], 10)
            self.assertTrue(kwargs["capture_output"])
            self.assertNotIn("shell", kwargs)
            if "-checkhost" in arguments:
                verb = 'does NOT' if zero_exit_mismatch else 'does'
                output = f'Hostname {arguments[-1]} {verb} match certificate\n'.encode('ascii')
            elif "-dates" in arguments:
                output = details.encode("ascii")
            elif "-pubkey" in arguments:
                output = b"public PEM"
            elif "-pubout" in arguments:
                self.assertIn("-passin", arguments)
                output = b"mismatched DER" if mismatch else b"public DER"
            else:
                output = b"public DER"
            code = 1 if reject_host and "-checkhost" in arguments else 0
            return subprocess.CompletedProcess(arguments, code, output, b"DO-NOT-EXPOSE-PRIVATE-MATERIAL")
        return run

    def test_certificate_result_contains_only_public_metadata(self):
        result = validate_certificate(self.cert, self.key, self.names, runner=self.runner())
        self.assertEqual(result["domains"], self.names)
        self.assertEqual(result["sha256"], "ab" * 32)
        self.assertIn("+00:00", result["not_after"])
        self.assertNotIn("private", json.dumps(result))

    def test_reject_mismatch_missing_san_expired_future_and_hostname_mismatch(self):
        for option in ("mismatch", "missing_san", "expired", "future", "reject_host", "zero_exit_mismatch"):
            with self.subTest(option=option), self.assertRaises(ValueError) as caught:
                validate_certificate(self.cert, self.key, self.names, runner=self.runner(**{option: True}))
            self.assertNotIn("DO-NOT-EXPOSE", str(caught.exception))

    def test_reject_invalid_inputs_without_running_openssl(self):
        def never_run(*args, **kwargs):
            self.fail("OpenSSL must not run for invalid arguments")
        for domains in ([], "site.example.test", ["site.example.test;bad"], None):
            with self.subTest(domains=domains), self.assertRaises(ValueError):
                validate_certificate(self.cert, self.key, domains, runner=never_run)
        with self.assertRaises(ValueError):
            validate_certificate(self.cert, self.key.with_name("missing.pem"), self.names, runner=never_run)

    def test_tool_failures_are_sanitized(self):
        def fail(*args, **kwargs):
            raise subprocess.TimeoutExpired("private material", 10, output=b"secret")
        with self.assertRaisesRegex(ValueError, "could not complete") as caught:
            validate_certificate(self.cert, self.key, self.names, runner=fail)
        self.assertNotIn("secret", str(caught.exception))

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is unavailable")
    def test_real_openssl_certificate_import_and_hostname_rejection(self):
        completed = subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(self.key), "-out", str(self.cert), "-days", "1",
            "-subj", "/CN=site.example.test", "-addext", "subjectAltName=DNS:site.example.test,DNS:sub.example.test",
        ], capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        result = validate_certificate(self.cert, self.key, self.names)
        self.assertEqual(result["domains"], self.names)
        self.assertEqual(len(result["sha256"]), 64)
        with self.assertRaises(ValueError):
            validate_certificate(self.cert, self.key, ["wrong.example.test"])


if __name__ == "__main__":
    unittest.main()
