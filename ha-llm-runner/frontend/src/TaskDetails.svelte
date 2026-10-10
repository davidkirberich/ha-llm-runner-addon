<script lang="ts">
  import { onMount } from 'svelte';
  import { busy, formatTime } from './api';
  import type { TaskDetail } from './types';
  import StatusChip from './StatusChip.svelte';
  import ResultView from './ResultView.svelte';
  import TaskConfig from './TaskConfig.svelte';

  let { detail, pending, fileBusy, openConfig, onremove, onaudit, onrun, onclear, ondirty, onsaved }: {
    detail: TaskDetail;
    pending: boolean;
    fileBusy: boolean;
    openConfig: boolean;
    onremove: (id: string) => void;
    onaudit: (id: string) => void;
    onrun: (id: string) => void;
    onclear: (id: string) => void;
    ondirty: (dirty: boolean) => void;
    onsaved: () => void;
  } = $props();
  let tab = $state<'details' | 'memory' | 'config'>('details');
  onMount(() => { if (openConfig) tab = 'config'; });
</script>

<section class="card">
  <div class="row">
    <h2 class="mono">{detail.id}</h2>
    <StatusChip status={detail.status} />
    <span class="grow"></span>
    <button class="danger" disabled={fileBusy || busy(detail.status.state)}
      title={busy(detail.status.state) ? 'Wait until this task has finished' : ''} onclick={() => onremove(detail.id)}>Remove</button>
    <button class="secondary" onclick={() => onaudit(detail.id)}>Audit archives</button>
    <button disabled={pending || busy(detail.status.state)} onclick={() => onrun(detail.id)}>Run now</button>
  </div>
  {#if detail.status.error}<div class="banner error">{detail.status.error}</div>{/if}
  <nav aria-label="Task details">
    <button class:active={tab === 'details'} onclick={() => tab = 'details'}>Details</button>
    <button class:active={tab === 'memory'} onclick={() => tab = 'memory'}>Memory ({detail.memory.length})</button>
    <button class:active={tab === 'config'} onclick={() => tab = 'config'}>Config</button>
  </nav>
  <div hidden={tab !== 'details'}>
    <h3>Last result <span class="muted">{formatTime(detail.status.finished_at)}</span></h3>
    {#if detail.status.last_result != null}
      <ResultView result={detail.status.last_result} />
    {:else}
      <p class="muted">No result since the add-on was started.</p>
    {/if}
    {#if detail.status.attachments?.length}
      <p class="muted">Attachments: {detail.status.attachments.join(', ')}</p>
    {/if}
    <h3>Last prompt</h3>
    {#if detail.status.last_prompt}
      <pre>{detail.status.last_prompt}</pre>
    {:else}
      <p class="muted">{detail.config.prompt ? 'Not run since the add-on was started.' : 'This task has no prompt (data only).'}</p>
    {/if}
  </div>
  <div hidden={tab !== 'memory'}>
    <div class="row">
      <h3>Memory</h3>
      <span class="muted">The newest {detail.config.history_limit ?? 7} are available as {'{history}'}</span>
      <span class="grow"></span>
      {#if detail.memory.length}
        <button class="danger" disabled={pending} onclick={() => onclear(detail.id)}>Clear memory</button>
      {/if}
    </div>
    {#each detail.memory as entry}
      <div class="memory-entry">
        <div class="muted">{entry.time || ''}</div>
        <div class="memory-text">{entry.text || ''}</div>
      </div>
    {:else}
      <p class="muted">Empty.</p>
    {/each}
  </div>
  <div hidden={tab !== 'config'}>
    <TaskConfig taskId={detail.id} savedText={detail.settings_text} {fileBusy} {ondirty} {onsaved} />
  </div>
</section>
