import type { Job, JsonObject } from './types';

let csrf = '';
export class ApiError extends Error {
  constructor(public code: string, message: string) { super(message); this.name = 'ApiError'; }
}
async function request<T>(path: string, payload?: unknown): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(path, {
      method: payload === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: payload === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      ...(payload === undefined ? {} : {body: JSON.stringify(payload)}), signal: controller.signal,
    });
    const body = await response.json().catch(() => null);
    if (response.status === 401 && path !== '/api/login') {
      csrf = ''; window.dispatchEvent(new Event('proxyhub:unauthorized'));
    }
    if (!response.ok || body?.ok === false) {
      throw new ApiError(body?.error?.code ?? String(response.status), body?.error?.message ?? '请求未完成，请重试。');
    }
    if (!body) throw new ApiError('invalid_response', '服务响应格式无效，请检查管理服务。');
    return body as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError('network_error', '无法连接管理服务。请检查 SSH 隧道后重试；已提交的任务可能仍在运行。');
  } finally { window.clearTimeout(timer); }
}
export async function session(): Promise<boolean> {
  const result = await request<{authenticated: boolean; csrf?: string}>('/api/session');
  csrf = result.csrf ?? ''; return result.authenticated;
}
export async function login(credential: string): Promise<void> {
  const result = await request<{authenticated: boolean; csrf: string}>('/api/login', {credential});
  if (!result.authenticated) throw new ApiError('login_failed', '管理凭据无效。');
  csrf = result.csrf;
}
export async function logout(): Promise<void> {
  try {await request('/api/logout', {});} finally {csrf = '';}
}
export async function operation<T>(operation: string, payload: JsonObject = {}): Promise<T> {
  const result = await request<{ok: true; result: T}>('/api/operation', {operation, payload});
  return result.result;
}
export async function awaitJob<T>(jobId: string, progress: (job: Job) => void): Promise<T> {
  for (let count = 0; count < 300; count++) {
    const job = await operation<Job>('job.get', {job_id: jobId});
    progress(job);
    if (job.status === 'succeeded') return job.result as T;
    if (job.status === 'failed' || job.status === 'interrupted') {
      const error = typeof job.error === 'string' ? {message: job.error} : job.error;
      throw new ApiError(error?.code ?? job.status, error?.message ?? '任务未完成，请在任务记录中查看状态并重新预览。');
    }
    await new Promise(resolve => window.setTimeout(resolve, count < 10 ? 1000 : 2000));
  }
  throw new ApiError('still_running', '任务仍在后台处理，请到任务记录查看结果。请勿重复提交。');
}
export function message(error: unknown): string {
  if (error instanceof ApiError && /stale|revision|expired/i.test(error.code)) return '配置版本已变化或预览已过期。请刷新数据后重新预览。';
  return error instanceof Error ? error.message : '操作未完成，请重试。';
}
