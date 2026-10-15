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

function compose(input: unknown): { yaml: string; raw: string; uri: string; uris: string[] } {
  const source = record(input);
  keys(source, ["node", "hy2", "overrides"], ["node"]);
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
    }, ...(hy2 === undefined ? [] : [hy2])],
    "proxy-groups": [{ name: "PROXY", type: "select", proxies: [name, ...(hy2 === undefined ? [] : [hy2.name])] }],
    rules: ["MATCH,PROXY"],
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
  return { yaml: stringify(config), raw: Buffer.from(`${uris.join("\n")}\n`, "utf8").toString("base64"), uri, uris };
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
