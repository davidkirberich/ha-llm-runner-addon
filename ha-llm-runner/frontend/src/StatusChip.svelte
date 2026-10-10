<script lang="ts">
  import { elapsed } from './api';
  import type { TaskStatus } from './types';
  let { status }: { status: TaskStatus } = $props();
  const labels: Record<string, string> = {
    ok: 'OK', error: 'Error', running: 'Running', queued: 'Queued', idle: 'Not run yet',
  };
  const phase = $derived(status.state || 'idle');
  const since = $derived(phase === 'running' ? status.started_at : phase === 'queued' ? status.queued_at : undefined);
  let now = $state(Date.now());
  $effect(() => {
    if (!since) return;
    now = Date.now();
    const timer = setInterval(() => { now = Date.now(); }, 1000);
    return () => clearInterval(timer);
  });
  const age = $derived(since ? elapsed(since, now) : '');
  const title = $derived(status.error || (since ? `${phase === 'running' ? 'Started' : 'Queued'} ${new Date(since).toLocaleString()}` : ''));
</script>

<span class="chip {phase}" {title}>{labels[phase] || phase}{age ? ` ${age}` : phase === 'running' ? '...' : ''}</span>
