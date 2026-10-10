<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { api, formatTime } from './api';
  let { tasks, filter = '' }: { tasks: string[]; filter?: string } = $props();
  type Archive = { name: string; task: string; time: string; size: number };
  type Member = { name: string; size: number; mime: string; content?: string; truncated?: boolean };
  let archives = $state<Archive[]>([]);
  let selected = $state('');
  let loading = $state(true);
  let opening = $state(false);
  const taskArchives = $derived(archives.filter((item) => item.task === filter)
    .sort((a, b) => Date.parse(b.time) - Date.parse(a.time)));
  const calls = $derived(taskArchives.slice(0, 50));
  let detail = $state<{ name: string; members: Member[] } | null>(null);
  let error = $state('');
  let deleting = $state(false);
  let request = 0;
  let disposed = false;
  const lifetime = new AbortController();
  function report(reason: unknown) {
    if (!disposed) error = reason instanceof Error ? reason.message : String(reason);
  }
  $effect(() => {
    filter;
    untrack(() => { request++; });
    selected = ''; detail = null; error = ''; opening = false;
  });
  async function open(name: string) {
    const token = ++request;
    detail = null; error = ''; opening = !!name;
    if (!name) return;
    try {
      const value = await api<{ name: string; members: Member[] }>('GET', `audit/${encodeURIComponent(name)}`, lifetime.signal);
      if (!disposed && token === request) { detail = value; error = ''; }
    } catch (reason) { if (token === request) report(reason); }
    finally { if (!disposed && token === request) opening = false; }
  }
  async function remove() {
    if (!detail || deleting || !confirm(`Delete ${detail.name}?`)) return;
    const name = detail.name;
    deleting = true;
    try {
      await api('DELETE', `audit/${encodeURIComponent(name)}`, lifetime.signal);
      if (disposed) return;
      archives = archives.filter((item) => item.name !== name);
      if (selected === name) { request++; selected = ''; detail = null; opening = false; }
    } catch (reason) { report(reason); }
    finally { if (!disposed) deleting = false; }
  }
  onMount(() => {
    void api<{ archives: Archive[] }>('GET', 'audit', lifetime.signal)
      .then((data) => { if (!disposed) archives = data.archives; })
      .catch(report)
      .finally(() => { if (!disposed) loading = false; });
    return () => { disposed = true; lifetime.abort(); };
  });
</script>
<section class="card">
  <div class="row"><h2>Audit</h2><label>Task <select aria-label="Audit task" bind:value={filter} disabled={loading || deleting}>
    <option value="">Select a task</option>
    {#each [...new Set([...tasks, ...archives.map((item) => item.task), ...(filter ? [filter] : [])])] as task}
      <option value={task}>{task}</option>
    {/each}
  </select></label>
  <label>Call <select aria-label="Audit call" bind:value={selected}
    disabled={!filter || loading || deleting || !calls.length} onchange={() => open(selected)}>
    <option value="">{loading ? 'Loading...' : filter && !calls.length ? 'No audit calls available' : 'Select a call (newest first)'}</option>
    {#each calls as archive (archive.name)}
      <option value={archive.name}>{formatTime(archive.time)}</option>
    {/each}
  </select></label></div>
  {#if error}<div class="banner error" role="alert">{error}</div>{/if}
  {#if taskArchives.length > 50}<p class="muted">Showing the 50 most recent calls.</p>{/if}
  {#if opening}<p class="muted" role="status">Loading audit call...</p>{/if}
</section>
{#if detail}
  <section class="card">
    <div class="row"><h2>{detail.name}</h2><span class="grow"></span>
      <a href="api/audit/{encodeURIComponent(detail.name)}/download" download>Download</a>
      <button class="danger" disabled={deleting} onclick={remove}>Delete</button>
    </div>
    {#each detail.members as member}
      <h3>{member.name} <span class="muted">{member.size < 1024 ? `${member.size} B` : `${(member.size / 1024).toFixed(1)} KB`}</span></h3>
      {#if member.content !== undefined}<pre>{member.content}{member.truncated ? '\n... (truncated; download the archive for the full file)' : ''}</pre>
      {:else if member.mime.startsWith('image/')}
        <img class="attachment" src="api/audit/{encodeURIComponent(detail.name)}/files/{encodeURIComponent(member.name)}" alt={member.name}>
      {:else}<a href="api/audit/{encodeURIComponent(detail.name)}/files/{encodeURIComponent(member.name)}" download={member.name}>Download {member.name}</a>{/if}
    {/each}
  </section>
{/if}
