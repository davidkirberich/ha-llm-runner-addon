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
  const data = await response.json();
  if (!response.ok) {
    throw new ApiError(typeof data.error === 'string' ? data.error : `${response.status} ${response.statusText}`, data);
  }
  return data;
}

export function formatTime(iso?: string): string {
  if (!iso) return '-';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

export function busy(state?: string): boolean {
  return state === 'running' || state === 'queued';
}
