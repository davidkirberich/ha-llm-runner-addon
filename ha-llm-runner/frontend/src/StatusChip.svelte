<script lang="ts">
  import type { TaskStatus } from './types';
  let { status }: { status: TaskStatus } = $props();
  const labels: Record<string, string> = {
    ok: 'OK', error: 'Error', running: 'Running...', queued: 'Queued', idle: 'Not run yet',
  };
  const state = $derived(status.state || 'idle');
</script>

<span class="chip {state}" title={status.error || ''}>{labels[state] || state}</span>
