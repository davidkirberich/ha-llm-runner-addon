import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, busy, elapsed, formatTime } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('API client', () => {
  it('keeps paths relative for Home Assistant Ingress', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"tasks":[]}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    expect(await api('GET', 'overview')).toEqual({ tasks: [] });
    expect(fetchMock.mock.calls[0][0]).toBe('api/overview');
  });

  it('sends JSON for actions', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"queued":["demo"]}'));
    vi.stubGlobal('fetch', fetchMock);
    await api('POST', 'tasks/demo/run');
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
  });

  it('surfaces backend errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"error":"Task missing"}', { status: 404 })));
    await expect(api('GET', 'tasks/missing')).rejects.toThrow('Task missing');
  });

  it('sends the exact unsaved editor content and retains validation details', async () => {
    const content = 'prompt: |\n  Unsaved prompt';
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"error":"Invalid","line":2,"warnings":["notice"]}', { status: 400 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api('POST', 'tasks/demo/preview', undefined, { content })).rejects.toMatchObject({
      data: { line: 2, warnings: ['notice'] },
    });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ content });
  });

  it('does not turn invalid JSON into a successful response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not JSON')));
    await expect(api('GET', 'overview')).rejects.toThrow('unexpected response');
  });

  it('explains non-JSON gateway errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>502 Bad Gateway</html>', { status: 502 })));
    await expect(api('GET', 'overview')).rejects.toThrow('not reachable');
  });
});

it('formats elapsed time', () => {
  const now = Date.parse('2024-01-01T12:00:00Z');
  expect(elapsed('2024-01-01T11:59:18Z', now)).toBe('42s');
  expect(elapsed('2024-01-01T11:56:55Z', now)).toBe('3m 05s');
  expect(elapsed('2024-01-01T10:58:00Z', now)).toBe('1h 02m');
  expect(elapsed('2024-01-01T12:00:05Z', now)).toBe('0s');
  expect(elapsed('invalid', now)).toBe('');
  expect(elapsed(undefined, now)).toBe('');
});

it('recognizes running and queued tasks', () => {
  expect(busy('running')).toBe(true);
  expect(busy('queued')).toBe(true);
  expect(busy('ok')).toBe(false);
  expect(busy()).toBe(false);
});

it('formats missing or invalid timestamps', () => {
  expect(formatTime()).toBe('-');
  expect(formatTime('invalid')).toBe('invalid');
});
