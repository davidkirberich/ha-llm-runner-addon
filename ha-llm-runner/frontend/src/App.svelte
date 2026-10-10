<script lang="ts">
  import { onMount } from 'svelte';
  import { api, busy, formatTime } from './api';
  import type { Overview, TaskDetail } from './types';
  import StatusChip from './StatusChip.svelte';
  import TaskDetails from './TaskDetails.svelte';
  import FileEditor from './FileEditor.svelte';
  import Audit from './Audit.svelte';

  let overview = $state<Overview | null>(null);
  let selected = $state<string | null>(null);
  let detail = $state<TaskDetail | null>(null);
  let error = $state('');
  let notice = $state('');
  let pending = $state(false);
  let settingsDirty = $state(false);
  let configDirty = $state(false);
  let processorDirty = $state(false);
  let tab = $state<'tasks' | 'config' | 'processors' | 'audit'>('tasks');
  let auditFilter = $state('');
  let openConfig = $state(false);
  let configOpened = $state(false);
  let processorsOpened = $state(false);
  let creatingTask = $state(false);
  let newTaskId = $state('');
  let configWorking = $state(false);
  let processorWorking = $state(false);
  let detailRequest = 0;
  let controller: AbortController;
  let timer: ReturnType<typeof setTimeout>;
  let noticeTimer: ReturnType<typeof setTimeout>;
  let refreshing = false;
  let refreshAgain = false;

  function report(reason: unknown) {
    if (!controller.signal.aborted) error = reason instanceof Error ? reason.message : String(reason);
  }
  function dismissNotice() {
    clearTimeout(noticeTimer);
    notice = '';
  }
  function notify(message: string) {
    dismissNotice();
    notice = message;
    noticeTimer = setTimeout(dismissNotice, 5000);
  }
  function switchTab(next: typeof tab) {
    if (next !== tab) dismissNotice();
    if (next === 'config') configOpened = true;
    if (next === 'processors') processorsOpened = true;
    tab = next;
  }

  async function loadDetail(id: string) {
    const request = ++detailRequest;
    const value = await api<TaskDetail>('GET', `tasks/${encodeURIComponent(id)}`, controller.signal);
    if (selected === id && request === detailRequest) detail = value;
  }

  async function refresh() {
    if (refreshing) {
      refreshAgain = true;
      return;
    }
    clearTimeout(timer);
    refreshing = true;
    try {
      overview = await api<Overview>('GET', 'overview', controller.signal);
      if (selected && overview.tasks.some((task) => task.id === selected)) {
        await loadDetail(selected);
      } else {
        selected = null;
        detail = null;
      }
      error = '';
    } catch (reason) {
      report(reason);
    } finally {
      refreshing = false;
      if (!controller.signal.aborted) {
        const delay = refreshAgain ? 0 : overview?.tasks.some((task) => busy(task.status.state)) ? 2000 : 15000;
        refreshAgain = false;
        timer = setTimeout(refresh, delay);
      }
    }
  }

  async function select(id: string) {
    if (selected === id) return;
    if (settingsDirty && !confirm('Discard your unsaved changes of the settings?')) return;
    settingsDirty = false;
    openConfig = false;
    selected = id;
    detail = null;
    try { await loadDetail(id); } catch (reason) { report(reason); }
  }

  const drafts = $derived(settingsDirty || configDirty || processorDirty);
  const fileBusy = $derived(pending || configWorking || processorWorking || (overview?.tasks.some((task) => busy(task.status.state)) ?? false));
  async function createTask() {
    if (drafts || fileBusy) { error = 'Save or discard editor changes and wait for running tasks before creating a task.'; return; }
    const id = newTaskId.trim();
    if (!id) return;
    pending = true;
    try {
      await api('POST', 'tasks', controller.signal, { id });
      creatingTask = false; newTaskId = '';
      selected = id; detail = null; openConfig = true; switchTab('tasks');
      await refresh();
      notify(`Task '${id}' created`);
    } catch (reason) { report(reason); }
    finally { pending = false; }
  }
  async function deleteTask(id: string) {
    if (fileBusy || configDirty || processorDirty) return;
    if (!confirm(`Remove '${id}' including its sensor, memory and audit archives?${settingsDirty ? ' Unsaved task changes will be lost.' : ''}`)) return;
    pending = true;
    try {
      await api('DELETE', `tasks/${encodeURIComponent(id)}`, controller.signal);
      selected = null; detail = null; settingsDirty = false;
      await refresh(); notify(`Task '${id}' removed`);
    } catch (reason) { report(reason); }
    finally { pending = false; }
  }
  function showAudit(id: string) { auditFilter = id; switchTab('audit'); }

  async function run(id?: string) {
    if (pending || configWorking || processorWorking) return;
    pending = true;
    try {
      if (id) {
        await api('POST', `tasks/${encodeURIComponent(id)}/run`, controller.signal);
        notify(`Task '${id}' started`);
      } else {
        const result = await api<{ queued: string[] }>('POST', 'run-all', controller.signal);
        notify(`${result.queued.length} task(s) queued`);
      }
      await refresh();
    } catch (reason) {
      report(reason);
    } finally {
      pending = false;
    }
  }

  async function clearMemory(id: string) {
    if (pending) return;
    if (!confirm(`Clear the memory of '${id}'?`)) return;
    pending = true;
    try {
      await api('DELETE', `tasks/${encodeURIComponent(id)}/memory`, controller.signal);
      notify(`Memory of '${id}' cleared`);
      await refresh();
    } catch (reason) {
      report(reason);
    } finally {
      pending = false;
    }
  }

  onMount(() => {
    controller = new AbortController();
    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
      clearTimeout(noticeTimer);
    };
  });
</script>

<svelte:window onbeforeunload={(event) => { if (drafts) { event.preventDefault(); event.returnValue = ''; } }} />
<header>
  <h1>HA LLM Runner</h1>
  <span class="chip" class:ok={overview?.mqtt.connected} class:error={overview && !overview.mqtt.connected}
    title={overview?.mqtt.error || ''}>
    MQTT {overview ? overview.mqtt.connected ? 'connected' : 'disconnected' : '...'}
  </span>
  <span class="grow"></span>
  <button disabled={!overview || pending} onclick={() => run()}>Run all tasks</button>
</header>
<nav class="main-nav" aria-label="Main navigation">
  {#each ['tasks', 'config', 'processors', 'audit'] as name}
    <button class:active={tab === name} onclick={() => {
      if (name === 'tasks' || name === 'config' || name === 'processors' || name === 'audit') switchTab(name);
    }}>{name === 'config' ? 'llm_tasks.yaml' : name.charAt(0).toUpperCase() + name.slice(1)}</button>
  {/each}
</nav>
<main>
  {#if error}
    <div class="banner error" role="alert">{error}</div>
  {/if}
  {#if notice}
    <div class="row banner success" role="status">{notice}<button class="secondary" onclick={dismissNotice}>Dismiss</button></div>
  {/if}
  {#if overview?.config_errors.length}
    <div class="banner error">
      llm_tasks.yaml has errors:
      {overview.config_errors.map((item) => `${item.line ? `line ${item.line}: ` : ''}${item.message}`).join(' / ')}
    </div>
  {:else if overview?.config_warnings.length}
    <div class="banner">{overview.config_warnings.join(' / ')}</div>
  {/if}
  <div hidden={tab !== 'tasks'}>
  <section class="card">
    <div class="row"><h2>Tasks</h2><span class="grow"></span>
      <button class="secondary" disabled={drafts || fileBusy} onclick={() => creatingTask = true}>New</button>
    </div>
    {#if creatingTask}
      <form class="row" onsubmit={(event) => { event.preventDefault(); void createTask(); }}>
        <label>Task ID <input class="search" required pattern={'[a-z0-9_]{1,64}'} maxlength="64" bind:value={newTaskId}></label>
        <button type="submit" disabled={fileBusy || drafts}>Create task</button>
        <button type="button" class="secondary" onclick={() => creatingTask = false}>Cancel</button>
      </form>
    {/if}
    {#if overview}
      <div class="table-scroll">
        <table>
          <thead><tr><th>Task</th><th>Status</th><th>Last run</th><th>Memory</th><th>Actions</th></tr></thead>
          <tbody>
            {#each overview.tasks as task (task.id)}
              <tr class:selected={selected === task.id}>
                <td>
                  <strong class="mono">{task.id}</strong>
                  <div class="muted">
                    {task.llm ? `${task.provider || 'default provider'}${task.model ? ` / ${task.model}` : ''}` : 'data only'}
                  </div>
                </td>
                <td><StatusChip status={task.status} /></td>
                <td class="muted">
                  {formatTime(task.status.finished_at)}
                  {#if task.status.duration != null} ({task.status.duration}s){/if}
                </td>
                <td class="muted">{task.memory_entries}</td>
                <td><div class="row">
                  <button class="secondary" onclick={() => select(task.id)}>Details</button>
                  <button class="secondary" disabled={pending || busy(task.status.state)} onclick={() => run(task.id)}>Run</button>
                </div></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if !overview.tasks.length}<p class="muted">No tasks yet. Click New or write them in llm_tasks.yaml.</p>{/if}
    {:else}
      <p class="muted">Loading tasks...</p>
    {/if}
  </section>
  {#if detail}
    {#key detail.id}
      <TaskDetails {detail} {pending} fileBusy={fileBusy || configDirty || processorDirty}
        {openConfig} onremove={deleteTask} onaudit={showAudit}
        onrun={run} onclear={clearMemory} ondirty={(dirty) => settingsDirty = dirty} onsaved={() => { void refresh(); }} />
    {/key}
  {:else if selected}
    <section class="card"><p class="muted">Loading task details...</p></section>
  {/if}
  </div>
  <div hidden={tab !== 'config'}>
    {#if configOpened}
      <FileEditor kind="config" active={tab === 'config'}
        blocked={pending || processorWorking || settingsDirty || processorDirty || (overview?.tasks.some((task) => busy(task.status.state)) ?? false)}
        onbusy={(value) => configWorking = value}
        ondirty={(value) => configDirty = value} onsaved={() => { void refresh(); }} />
    {/if}
  </div>
  <div hidden={tab !== 'processors'}>
    {#if processorsOpened}
      <FileEditor kind="processors" active={tab === 'processors'}
        blocked={pending || configWorking || settingsDirty || configDirty || (overview?.tasks.some((task) => busy(task.status.state)) ?? false)}
        onbusy={(value) => processorWorking = value}
        ondirty={(value) => processorDirty = value} onsaved={() => { void refresh(); }} />
    {/if}
  </div>
  {#if tab === 'audit'}<Audit tasks={overview?.tasks.map((task) => task.id) || []} filter={auditFilter} />{/if}
</main>
