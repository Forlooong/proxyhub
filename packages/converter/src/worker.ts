/** One bounded stdin document in; one JSON result out. No network or filesystem IO. */
import { isIP } from "node:net";
import { parseDocument, stringify } from "yaml";

type RecordValue = Record<string, unknown>;
const INPUT_LIMIT = 64 * 1024;

class InputError extends Error {}

function record(value: unknown): RecordValue {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new InputError("Expected an object");
  }
  return value as RecordValue;
}

function keys(value: RecordValue, allowed: string[], required = allowed): void {
  if (Object.keys(value).some((key) => !allowed.includes(key)) ||
      required.some((key) => !Object.hasOwn(value, key))) {
    throw new InputError("Unknown or missing field");
  }
}

function string(value: unknown, maximum: number): string {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum ||
      /[\u0000-\u001f\u007f]/u.test(value)) {
    throw new InputError("Invalid text field");
  }
  return value;
}

function integer(value: unknown, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) {
    throw new InputError("Invalid integer field");
  }
  return value;
}

function hostname(value: unknown, ipAllowed: boolean): string {
  const host = string(value, 253);
  if (ipAllowed && isIP(host)) return host;
  if (!host.split(".").every((part) => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/u.test(part))) {
    throw new InputError("Invalid server name");
  }
  return host;
}

const RESERVED_NAMES = ["PROXY", "DIRECT", "REJECT", "GLOBAL", "PASS", "REJECT-DROP", "COMPATIBLE"];
const SS_METHODS = ["aes-128-gcm", "aes-256-gcm", "chacha20-ietf-poly1305",
  "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305"];

function identifier(value: unknown): string {
  const result = string(value, 64);
  if (!/^[A-Za-z0-9_-]+$/u.test(result)) throw new InputError("Invalid identifier");
  return result;
}

function resourceIdentifier(value: unknown, defaultAllowed = true): string {
  const result = identifier(value);
  if (!/^[0-9a-f]{32}$/u.test(result) && !(defaultAllowed && result === "default")) throw new InputError("Invalid resource identifier");
  return result;
}

function dnsName(value: unknown): string {
  const host = hostname(value, false);
  const labels = host.toLowerCase().split(".");
  if (labels.length < 2 || !/[a-z]/u.test(labels.at(-1) as string) || ["local", "localhost"].includes(labels.at(-1) as string)) {
    throw new InputError("Invalid DNS name");
  }
  return host;
}

function uuidCredential(value: unknown): string {
  const result = string(value, 36);
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu.test(result)) {
    throw new InputError("Invalid credential");
  }
  return result;
}

function passwordCredential(value: unknown): string {
  const result = string(value, 128);
  if (!/^[A-Za-z0-9_-]{32,128}$/u.test(result)) throw new InputError("Invalid credential");
  return result;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new InputError("Invalid boolean");
  return value;
}

function choice(value: unknown, allowed: string[]): string {
  if (typeof value !== "string" || !allowed.includes(value)) throw new InputError("Unsupported option");
  return value;
}

function queryString(parameters: Record<string, string>): string {
  return Object.entries(parameters).map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`).join("&");
}

/** Consume a managed snapshot, never an imported URI or arbitrary Mihomo object. */
function extraNode(value: unknown): { id: string; proxy: RecordValue; uri: string } {
  const source = record(value);
  keys(source, ["id", "protocol", "name", "server", "port", "options", "credentials"]);
  const id = resourceIdentifier(source.id, false);
  const protocol = choice(source.protocol, ["ss", "trojan", "vmess", "vless", "socks5", "tuic", "anytls"]);
  const name = string(source.name, 120);
  if ([...RESERVED_NAMES, "节点选择"].includes(name) || !name.trim() || name.includes(",")) throw new InputError("Invalid name");
  const server = hostname(source.server, true);
  const port = integer(source.port, 1, 65535);
  const options = record(source.options);
  const credentials = record(source.credentials);
  const proxy: RecordValue = { name, type: protocol, server, port, udp: true };
  const authority = `${isIP(server) === 6 ? `[${server}]` : server}:${port}`;
  const label = encodeURIComponent(name);
  let user: string;
  let parameters: Record<string, string> = {};
  if (protocol === "ss") {
    keys(options, ["method"]);
    keys(credentials, ["password"]);
    const method = choice(options.method, SS_METHODS);
    let password: string;
    if (method.startsWith("2022-")) {
      password = string(credentials.password, 44);
      const decoded = Buffer.from(password, "base64");
      if (decoded.length !== (method === "2022-blake3-aes-128-gcm" ? 16 : 32) || decoded.toString("base64") !== password) {
        throw new InputError("Invalid 2022 key");
      }
    } else password = passwordCredential(credentials.password);
    Object.assign(proxy, { cipher: method, password });
    // SIP002 requires plaintext userinfo for AEAD-2022; older AEAD uses URL-safe Base64.
    user = method.startsWith("2022-") ? `${encodeURIComponent(method)}:${encodeURIComponent(password)}`
      : Buffer.from(`${method}:${password}`, "utf8").toString("base64url");
    return { id, proxy, uri: `ss://${user}@${authority}#${label}` };
  }
  if (protocol === "socks5") {
    keys(options, ["auth", "udp"]);
    const auth = boolean(options.auth);
    proxy.udp = boolean(options.udp);
    keys(credentials, auth ? ["username", "password"] : []);
    user = "";
    if (auth) {
      const username = string(credentials.username, 64);
      if (!/^[A-Za-z0-9_-]{8,64}$/u.test(username)) throw new InputError("Invalid username");
      const password = passwordCredential(credentials.password);
      Object.assign(proxy, { username, password });
      // This is the Base64 userinfo convention consumed by the pinned Mihomo importer.
      user = `${Buffer.from(`${username}:${password}`).toString("base64url")}@`;
    }
    return { id, proxy, uri: `socks5://${user}${authority}?udp=${proxy.udp ? "1" : "0"}#${label}` };
  }
  const stream = ["vless", "vmess", "trojan"].includes(protocol);
  const transport = stream ? choice(options.transport, protocol === "vmess"
    ? ["tcp", "http", "ws", "h2", "httpupgrade"] : ["tcp", "ws", "h2", "httpupgrade"]) : "tcp";
  if (protocol === "trojan" && transport === "h2") throw new InputError("Unsupported Trojan transport");
  const tls = boolean(options.tls);
  if ((!stream || protocol === "trojan" || transport === "h2") && !tls) throw new InputError("TLS is required");
  const mode = tls ? choice(options.tls_mode, protocol === "vless" ? ["certificate", "reality"] : ["certificate"]) : "none";
  const allowed = [...(stream ? ["transport"] : []), "tls",
    ...(tls ? ["tls_mode", "sni", mode === "reality" ? "reality_target_id" : "certificate_id"] : []),
    ...(transport !== "tcp" ? ["host", "path"] : []),
    ...(protocol === "vless" ? ["flow"] : []), ...(protocol === "vmess" ? ["security"] : []),
    ...(protocol === "tuic" ? ["congestion_control", "udp_relay_mode"] : [])];
  keys(options, allowed);
  if (tls) {
    const sni = dnsName(options.sni);
    resourceIdentifier(mode === "reality" ? options.reality_target_id : options.certificate_id);
    Object.assign(proxy, { [stream && protocol !== "trojan" ? "servername" : "sni"]: sni, "skip-cert-verify": false });
    parameters = { security: mode === "reality" ? "reality" : "tls", sni };
  } else parameters = { security: "none" };
  if (stream) {
    Object.assign(proxy, { network: transport === "httpupgrade" ? "ws" : transport, tls });
    // UDP through the pinned Mihomo H2 combination has not passed acceptance.
    // Do not advertise UDP support; this product promises H2 TCP only.
    if (transport === "h2") proxy.udp = false;
    parameters.type = transport;
    if (transport !== "tcp") {
      const host = dnsName(options.host);
      const path = string(options.path, 256);
      if (!path.startsWith("/") || /[^\x21-\x7e]|[?#]/u.test(path)) throw new InputError("Invalid transport path");
      parameters.host = host;
      parameters.path = path;
      if (transport === "h2") proxy["h2-opts"] = { host: [host], path };
      else if (transport === "http") proxy["http-opts"] = { method: "GET", path: [path], headers: { Host: [host] } };
      else proxy["ws-opts"] = { path, headers: { Host: host }, ...(transport === "httpupgrade" ? { "v2ray-http-upgrade": true } : {}) };
    }
  }
  if (protocol === "vless") {
    keys(credentials, mode === "reality" ? ["uuid", "public_key", "short_id"] : ["uuid"]);
    user = uuidCredential(credentials.uuid);
    const flow = choice(options.flow, ["", "xtls-rprx-vision"]);
    if (flow && (!tls || transport !== "tcp")) throw new InputError("Incompatible flow");
    Object.assign(proxy, { uuid: user, flow, "packet-encoding": "xudp" });
    parameters.encryption = "none";
    parameters.packetEncoding = "xudp";
    if (flow) parameters.flow = flow;
    if (mode === "reality") {
      if (!["tcp", "h2"].includes(transport)) throw new InputError("Incompatible REALITY transport");
      const publicKey = string(credentials.public_key, 43);
      if (!/^[A-Za-z0-9_-]{43}$/u.test(publicKey) || Buffer.from(publicKey, "base64url").toString("base64url") !== publicKey ||
          typeof credentials.short_id !== "string" || !/^(?:[0-9a-f]{2}){0,8}$/iu.test(credentials.short_id)) {
        throw new InputError("Invalid REALITY credential");
      }
      proxy["reality-opts"] = { "public-key": publicKey, "short-id": credentials.short_id };
      proxy["client-fingerprint"] = "chrome";
      Object.assign(parameters, { pbk: publicKey, sid: credentials.short_id, fp: "chrome" });
    }
  } else if (protocol === "vmess") {
    keys(credentials, ["uuid"]);
    user = uuidCredential(credentials.uuid);
    const security = choice(options.security, ["auto", "none", "aes-128-gcm", "chacha20-poly1305"]);
    if (transport === "http" && (tls || security !== "none")) throw new InputError("Incompatible HTTP transport");
    Object.assign(proxy, { uuid: user, alterId: 0, cipher: security, xudp: true });
    const document = { v: "2", ps: name, add: server, port: String(port), id: user, aid: "0", scy: security,
      net: transport === "http" ? "tcp" : transport, type: transport === "http" ? "http" : "none",
      host: transport === "tcp" ? "" : options.host, path: transport === "tcp" ? "" : options.path,
      tls: tls ? "tls" : "", sni: tls ? options.sni : "" };
    return { id, proxy, uri: `vmess://${Buffer.from(JSON.stringify(document), "utf8").toString("base64")}` };
  } else if (protocol === "tuic") {
    keys(credentials, ["uuid", "password"]);
    const uuid = uuidCredential(credentials.uuid);
    const password = passwordCredential(credentials.password);
    const congestion = choice(options.congestion_control, ["bbr", "cubic", "new_reno"]);
    const relay = choice(options.udp_relay_mode, ["native", "quic"]);
    Object.assign(proxy, { uuid, password, "congestion-controller": congestion, "udp-relay-mode": relay, alpn: ["h3"] });
    user = `${encodeURIComponent(uuid)}:${encodeURIComponent(password)}`;
    parameters = { sni: options.sni as string, alpn: "h3", congestion_control: congestion, udp_relay_mode: relay };
  } else {
    keys(credentials, ["password"]);
    const password = passwordCredential(credentials.password);
    proxy.password = password;
    user = encodeURIComponent(password);
    if (protocol === "anytls") parameters = { sni: options.sni as string };
  }
  return { id, proxy, uri: `${protocol}://${user}@${authority}?${queryString(parameters)}#${label}` };
}

function compose(input: unknown): { yaml: string; raw: string; uri: string; uris: string[]; proxies: RecordValue[] } {
  const source = record(input);
  keys(source, ["node", "hy2", "nodes", "overrides"], ["node"]);
  const node = record(source.node);
  keys(node, ["id", "name", "server", "port", "uuid", "server_name", "public_key", "short_id"]);
  const id = string(node.id, 64);
  if (!/^[A-Za-z0-9_-]+$/u.test(id)) throw new InputError("Invalid node identifier");
  const name = string(node.name, 200);
  if (["PROXY", "DIRECT", "REJECT", "GLOBAL", "PASS", "REJECT-DROP", "COMPATIBLE"].includes(name) || /[,\r\n]/u.test(name)) {
    throw new InputError("Reserved or invalid node name");
  }
  const server = hostname(node.server, true);
  const port = integer(node.port, 1, 65535);
  const uuid = string(node.uuid, 36);
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu.test(uuid)) {
    throw new InputError("Invalid node credential");
  }
  const serverName = hostname(node.server_name, false);
  const publicKey = string(node.public_key, 43);
  if (!/^[A-Za-z0-9_-]{43}$/u.test(publicKey) ||
      Buffer.from(publicKey, "base64url").toString("base64url") !== publicKey) {
    throw new InputError("Invalid REALITY key");
  }
  if (typeof node.short_id !== "string" || !/^(?:[0-9a-f]{2}){0,8}$/iu.test(node.short_id)) {
    throw new InputError("Invalid REALITY short identifier");
  }
  let hy2: RecordValue | undefined;
  if (Object.hasOwn(source, "hy2")) {
    const inputHy2 = record(source.hy2);
    keys(inputHy2, ["id", "name", "server", "port", "password", "server_name"]);
    if (inputHy2.id !== "local-hy2") throw new InputError("Invalid HY2 node identifier");
    const hy2Name = string(inputHy2.name, 120);
    if ([name, "PROXY", "DIRECT", "REJECT", "GLOBAL", "PASS", "REJECT-DROP", "COMPATIBLE"].includes(hy2Name) || hy2Name.includes(",")) {
      throw new InputError("Reserved or duplicate HY2 node name");
    }
    const password = string(inputHy2.password, 128);
    if (!/^[A-Za-z0-9_-]{32,128}$/u.test(password)) throw new InputError("Invalid HY2 credential");
    const hy2Sni = hostname(inputHy2.server_name, false);
    const labels = hy2Sni.toLowerCase().split(".");
    const suffix = labels[labels.length - 1] as string;
    if (labels.length < 2 || !/[a-z]/u.test(suffix) || ["local", "localhost"].includes(suffix)) {
      throw new InputError("Invalid HY2 server name");
    }
    hy2 = {
      name: hy2Name, type: "hysteria2", server: hostname(inputHy2.server, true),
      port: integer(inputHy2.port, 1, 65535), password,
      sni: hy2Sni, "skip-cert-verify": false,
    };
  }
  if (Object.hasOwn(source, "nodes") && (!Array.isArray(source.nodes) || source.nodes.length > 32)) {
    throw new InputError("Invalid node count");
  }
  const extras = (source.nodes as unknown[] | undefined ?? []).map(extraNode);
  const ids = new Set([id, ...(hy2 === undefined ? [] : ["local-hy2"])]);
  const names = new Set([name, ...(hy2 === undefined ? [] : [hy2.name as string])]);
  for (const extra of extras) {
    if (ids.has(extra.id) || names.has(extra.proxy.name as string)) throw new InputError("Duplicate node");
    ids.add(extra.id);
    names.add(extra.proxy.name as string);
  }
  const overrides = source.overrides === undefined ? {} : record(source.overrides);
  keys(overrides, ["mixed-port", "log-level", "mode", "ipv6", "allow-lan"], []);
  const mixedPort = overrides["mixed-port"] === undefined ? 17890 : integer(overrides["mixed-port"], 1, 65535);
  const logLevel = Object.hasOwn(overrides, "log-level") ? overrides["log-level"] : "warning";
  if (!["silent", "error", "warning", "info", "debug"].includes(logLevel as string)) {
    throw new InputError("Invalid log level override");
  }
  const mode = Object.hasOwn(overrides, "mode") ? overrides.mode : "rule";
  if (!["rule", "global", "direct"].includes(mode as string)) throw new InputError("Invalid mode override");
  const ipv6 = Object.hasOwn(overrides, "ipv6") ? overrides.ipv6 : false;
  if (typeof ipv6 !== "boolean" || (Object.hasOwn(overrides, "allow-lan") && overrides["allow-lan"] !== false)) {
    throw new InputError("Invalid network override");
  }
  // Literal-IP resolvers bootstrap both resolver and node names without routing through PROXY.
  const config = {
    "mixed-port": mixedPort,
    "allow-lan": false,
    "bind-address": "127.0.0.1",
    mode,
    "log-level": logLevel,
    ipv6,
    tun: { enable: false },
    dns: {
      enable: true,
      ipv6,
      "enhanced-mode": "redir-host",
      "respect-rules": false,
      "default-nameserver": ["1.1.1.1", "8.8.8.8"],
      "proxy-server-nameserver": ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"],
      nameserver: ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"],
    },
    proxies: [{
      name, type: "vless", server, port, uuid, network: "tcp", udp: true,
      tls: true, servername: serverName, flow: "xtls-rprx-vision",
      "client-fingerprint": "chrome", "packet-encoding": "xudp",
      "reality-opts": { "public-key": publicKey, "short-id": node.short_id },
    }, ...(hy2 === undefined ? [] : [hy2]), ...extras.map((extra) => extra.proxy)],
    "proxy-groups": [{ name: "PROXY", type: "select", proxies: [name, ...(hy2 === undefined ? [] : [hy2.name]), ...extras.map((extra) => extra.proxy.name)] }],
    rules: ["MATCH,PROXY", ...(extras.some((extra) => extra.proxy.udp === false) ? ["MATCH,REJECT"] : [])],
  };
  const parameters: Record<string, string> = {
    encryption: "none", security: "reality", type: "tcp", sni: serverName,
    fp: "chrome", pbk: publicKey, sid: node.short_id,
    flow: "xtls-rprx-vision", packetEncoding: "xudp",
  };
  const query = Object.entries(parameters).map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`).join("&");
  const authority = isIP(server) === 6 ? `[${server}]` : server;
  const uri = `vless://${encodeURIComponent(uuid)}@${authority}:${port}?${query}#${encodeURIComponent(name)}`;
  const uris = [uri];
  if (hy2 !== undefined) {
    const hy2Server = hy2.server as string;
    const hy2Authority = isIP(hy2Server) === 6 ? `[${hy2Server}]` : hy2Server;
    uris.push(`hysteria2://${encodeURIComponent(hy2.password as string)}@${hy2Authority}:${hy2.port}/?sni=${encodeURIComponent(hy2.sni as string)}#${encodeURIComponent(hy2.name as string)}`);
  }
  uris.push(...extras.map((extra) => extra.uri));
  return { yaml: stringify(config), raw: Buffer.from(`${uris.join("\n")}\n`, "utf8").toString("base64"), uri, uris, proxies: config.proxies };
}

async function main(): Promise<void> {
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const value of process.stdin) {
    const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
    length += chunk.length;
    if (length > INPUT_LIMIT) throw new InputError("Input too large");
    chunks.push(chunk);
  }
  const decoded = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  const input: unknown = JSON.parse(decoded);
  // JSON.parse otherwise silently takes the last duplicate key (including escaped keys).
  if (parseDocument(decoded, { uniqueKeys: true }).errors.length > 0) {
    throw new InputError("Invalid or duplicate JSON field");
  }
  const result = compose(input);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch(() => {
  // Never echo source input, URIs, keys, or parser exceptions.
  process.stderr.write("converter: invalid or unsupported input\n");
  process.exitCode = 2;
});
