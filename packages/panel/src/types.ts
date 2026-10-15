export type Page = 'overview' | 'server' | 'site' | 'proxy' | 'subscription' | 'jobs' | 'settings';
export type Theme = 'light' | 'dark';
export type SSHContext = { addr: string; host: string; laddr: string; lport: string | number };
export type JsonObject = Record<string, unknown>;
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted';
export type Job = {
  id: string; operation: string; status: JobStatus; created_at: string | number; finished_at?: string | number | null;
  result?: JsonObject; error?: {code?: string; message?: string} | string;
};
export type Preview = {
  plan_id: string; kind: string; base_revision: number; summary: string;
  changes: {label: string; before: unknown; after: unknown}[];
  impact: string[]; warnings: string[];
};
export type Website = {
  id: string; listener_id: string; tls_port: number; domains: string[]; title: string; enabled: boolean;
  certificate_id: string | null; content_version: string | null; url: string | null; dependencies: string[];
};
export type Certificate = {
  id: string; name: string; domains: string[]; not_after: string | null; sha256: string | null;
  status: 'valid' | 'invalid'; error: string | null; automatic_renewal: boolean;
  consumers: {kind: string; id: string; label: string; domain: string; port: number; enabled?: boolean}[];
};
export type RealityEndpoint = {
  id: string; listener_id: string; listen_port: number; target_id: string; sni: string; primary: boolean;
  target_type: 'site' | 'external'; site_id: string | null; address: string; port: number;
};
export type Overview = {
  revision: number;
  server: {hostname: string; os: string; architecture: string; memory_total_bytes: number | null; accounts: string[]; ssh_context: SSHContext};
  website: {id: string; domain: string; title: string; url: string; content_version: string | null};
  certificate: {id: string; domains: string[]; not_after: string | null; sha256: string | null; automatic_renewal: boolean; consumers: string[]; status?: 'valid' | 'invalid'; error?: string | null};
  websites: Website[];
  certificates: Certificate[];
  proxy: {id: string; name: string; protocol: string; public_address: string; port: number; sni: string; target: string; verification: string};
  reality: {node_id: string; primary_endpoint_id: string; endpoints: RealityEndpoint[]};
  subscription: {id: string; name: string; domain: string; update_interval_hours: number; userinfo: Record<string, number>; reset_day: number | null; reset_at: number | null; overrides: JsonObject; traffic_source: string};
  services: {name: string; status: string}[];
  capabilities: {name: string; available: boolean; reason: string}[];
  recent_jobs: Job[];
};
export type Prepare = (kind: string, values: JsonObject) => Promise<void>;
export type FormProps = {data: Overview; prepare: Prepare; busy: boolean; notify: (message: string) => void};
