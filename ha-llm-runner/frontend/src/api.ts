export interface ApiProblem {
  error?: string;
  message?: string;
  line?: number;
  column?: number;
  errors?: { message: string; line?: number; column?: number }[];
  warnings?: string[];
}

export class ApiError extends Error {
  constructor(message: string, public readonly data: ApiProblem) {
    super(message);
  }
}

export async function api<T>(method: string, path: string, signal?: AbortSignal, body: unknown = {}): Promise<T> {
  const response = await fetch(`api/${path}`, {
    method,
    signal,
    ...(method === 'GET' ? {} : {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  });
  const raw = await response.text();
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new ApiError(response.ok
      ? 'The add-on sent an unexpected response. Reload the page.'
      : `The add-on is not reachable right now (${response.status}${response.statusText ? ` ${response.statusText}` : ''}). It may be restarting.`, {});
  }
  if (!response.ok) {
    const problem = (data && typeof data === 'object' ? data : {}) as ApiProblem;
    throw new ApiError(typeof problem.error === 'string' ? problem.error : `${response.status} ${response.statusText}`, problem);
  }
  return data as T;
}

export function formatTime(iso?: string): string {
  if (!iso) return '-';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

export function busy(state?: string): boolean {
  return state === 'running' || state === 'queued';
}

/** Compact time since an ISO timestamp, e.g. "42s", "3m 05s", "1h 02m"; empty if unknown. */
export function elapsed(iso: string | undefined, now: number): string {
  const since = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(since)) return '';
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  const pad = (value: number) => String(value).padStart(2, '0');
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${pad(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${pad(Math.floor(seconds / 60) % 60)}m`;
}
