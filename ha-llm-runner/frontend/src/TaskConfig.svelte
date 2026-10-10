<script lang="ts">
  import { onMount, tick, untrack } from 'svelte';
  import { api, ApiError } from './api';
  import { defaultAlias, insertIndent, lineOffset, openMoreInfo } from './editor';
  import type { EntityRow, EntityRows, Preview, SearchEntity, YamlCheck } from './types';
  import ResultView from './ResultView.svelte';

  let { taskId, savedText, fileBusy, ondirty, onsaved }: {
    taskId: string;
    savedText: string;
    fileBusy: boolean;
    ondirty: (dirty: boolean) => void;
    onsaved: () => void;
  } = $props();
  let text = $state('');
  let baseline = $state('');
  let dirty = $derived(text !== baseline);
  let check = $state<YamlCheck | null>(null);
  let entities = $state<EntityRows | null>(null);
  let checking = $state(true);
  let saving = $state(false);
  let editingEntities = $state(false);
  let previewBusy = $state(false);
  let preview = $state<Preview | null>(null);
  let previewText = $state('');
  let notice = $state('');
  let errors = $state<{ message: string; line?: number; column?: number }[]>([]);
  let warnings = $state<string[]>([]);
  let search = $state('');
  let matches = $state<SearchEntity[]>([]);
  let total = $state(0);
  let searchError = $state('');
  let aliases = $state<Record<string, string>>({});
  let editor: HTMLTextAreaElement;
  let disposed = false;
  const lifetime = new AbortController();
  const usable = $derived(!checking && check?.valid === true);

  $effect(() => {
    const value = savedText;
    untrack(() => {
      if (!dirty && !saving) text = baseline = value;
    });
  });
  $effect(() => { ondirty(dirty); });
  $effect(() => {
    const content = text;
    const controller = new AbortController();
    let active = true;
    checking = true;
    const timer = setTimeout(async () => {
      try {
        const result = await api<YamlCheck>('POST', 'yaml/check', controller.signal, { content, entities: true });
        if (!active) return;
        check = result;
        if (result.entities) entities = result.entities;
      } catch (reason) {
        if (!active) return;
        check = null;
        errors = [{ message: `YAML check failed: ${message(reason)}` }];
      } finally {
        if (active) checking = false;
      }
    }, 400);
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  });
  $effect(() => {
    const query = search.trim();
    const controller = new AbortController();
    let active = true;
    matches = [];
    total = 0;
    searchError = '';
    const timer = setTimeout(async () => {
      if (query.length < 2) return;
      try {
        const result = await api<{ entities: SearchEntity[]; total: number }>(
          'GET', `entities?q=${encodeURIComponent(query)}`, controller.signal);
        if (!active) return;
        matches = result.entities;
        total = result.total;
      } catch (reason) {
        if (active) searchError = message(reason);
      }
    }, 300);
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  });

  function message(reason: unknown): string {
    return reason instanceof Error ? reason.message : String(reason);
  }
  function report(reason: unknown) {
    const data = reason instanceof ApiError ? reason.data : {};
    errors = data.errors?.length
      ? data.errors.map((item) => ({ message: `${item.line ? `llm_tasks.yaml line ${item.line}: ` : ''}${item.message}` }))
      : [{ message: data.message || message(reason), line: data.line, column: data.column }];
    warnings = data.warnings || [];
  }
  function goToLine(line?: number, column?: number) {
    if (!line) return;
    const offset = lineOffset(text, line, column);
    editor.focus();
    editor.setSelectionRange(offset, offset);
    editor.scrollTop = Math.max(0, (line - 5) * (parseFloat(getComputedStyle(editor).lineHeight) || 18));
  }
  async function keydown(event: KeyboardEvent) {
    if (event.key === 'Tab' && !event.ctrlKey && !event.altKey && !event.shiftKey) {
      event.preventDefault();
      const value = insertIndent(text, editor.selectionStart, editor.selectionEnd);
      text = value.text;
      await tick();
      editor.setSelectionRange(value.cursor, value.cursor);
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      void save();
    } else if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      void runPreview();
    }
  }
  async function save() {
    if (!dirty || !usable || saving || editingEntities || fileBusy) return;
    const content = text;
    saving = true;
    errors = [];
    try {
      const result = await api<{ warnings: string[] }>('PUT',
        `tasks/${encodeURIComponent(taskId)}/settings`, lifetime.signal, { content });
      const fresh = await api<{ settings_text: string }>('GET',
        `tasks/${encodeURIComponent(taskId)}`, lifetime.signal);
      if (disposed) return;
      baseline = fresh.settings_text;
      if (text === content) text = fresh.settings_text;
      warnings = result.warnings;
      notice = text === baseline ? 'Task saved' : 'Snapshot saved; newer editor changes are still unsaved.';
      onsaved();
    } catch (reason) {
      if (!disposed) report(reason);
    } finally {
      if (!disposed) saving = false;
    }
  }
  async function discard() {
    if (!dirty || saving || editingEntities || !confirm('Discard your unsaved changes of the settings?')) return;
    const content = text;
    try {
      const fresh = await api<{ settings_text: string }>('GET',
        `tasks/${encodeURIComponent(taskId)}`, lifetime.signal);
      if (disposed) return;
      if (text !== content) {
        errors = [{ message: 'The YAML changed while reloading. Please retry Discard.' }];
        return;
      }
      text = baseline = fresh.settings_text;
      errors = [];
      warnings = [];
      notice = '';
    } catch (reason) { if (!disposed) report(reason); }
  }
  async function runPreview() {
    if (!usable || previewBusy || editingEntities) return;
    const content = text;
    previewText = content;
    previewBusy = true;
    preview = null;
    errors = [];
    try {
      const result = await api<Preview>('POST', `tasks/${encodeURIComponent(taskId)}/preview`,
        lifetime.signal, { content });
      if (disposed) return;
      preview = result;
      warnings = result.warnings || [];
    } catch (reason) {
      if (disposed) return;
      report(reason);
      preview = { ok: false, error: message(reason) };
    } finally {
      if (!disposed) previewBusy = false;
    }
  }
  async function changeEntities(change: { add?: { alias: string; entity_id: string }; remove?: string; section?: string }) {
    if (!usable || editingEntities || saving) return;
    const content = text;
    editingEntities = true;
    try {
      const result = await api<{ content: string }>('POST', 'yaml/entities', lifetime.signal, { content, ...change });
      if (disposed) return;
      if (text !== content) {
        errors = [{ message: 'The YAML changed while editing entities. Please retry Add/Remove.' }];
        return;
      }
      text = result.content;
      errors = [];
      const alias = change.add?.alias || change.remove;
      notice = change.remove && alias && !/^\d+$/.test(alias) && text.includes(`{${alias}}`)
        ? `Removed ${alias} - the prompt still uses {${alias}}`
        : `${change.add ? 'Added' : 'Removed'} ${alias || change.add?.entity_id}. Save to write it to llm_tasks.yaml.`;
    } catch (reason) { if (!disposed) report(reason); }
    finally { if (!disposed) editingEntities = false; }
  }
  function known(id: string) {
    return entities?.rows.some((row) => row.section === 'entities' && row.entity_id === id);
  }
  function value(row: EntityRow): string {
    const content = `${row.state ?? ''}${row.unit ? ` ${row.unit}` : ''}`;
    return content.length > 120 ? content.slice(0, 120) + '...' : content;
  }
  onMount(() => () => {
    disposed = true;
    lifetime.abort();
    ondirty(false);
  });
</script>

<svelte:window onbeforeunload={(event) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } }} />

<div class="row">
  <h3>Entities</h3><span class="muted">The table follows the task YAML below.</span>
  <span class="grow"></span>
  <button class="secondary" disabled={checking} onclick={async () => {
    const content = text;
    try {
      const result = await api<YamlCheck>('POST', 'yaml/check', lifetime.signal, { content, entities: true });
      if (!disposed && text === content) { check = result; entities = result.entities || null; }
    } catch (reason) { if (!disposed) report(reason); }
  }}>Refresh</button>
</div>
<div class:stale={!usable || !check?.entities}>
  {#if entities?.error}<div class="banner error">{entities.error}</div>{/if}
  {#if entities?.rows.length}
    <div class="table-scroll"><table>
      <thead><tr><th>Alias</th><th>Entity / URL</th><th>Name</th><th>Current value</th><th>Actions</th></tr></thead>
      <tbody>
        {#each entities.rows as row (`${row.section}:${row.alias}`)}
          <tr>
            <td class="mono">{row.alias}{row.section !== 'entities' ? ` (${row.section})` : ''}</td>
            <td class="mono">
              {#if row.entity_id && !row.missing}
                <a href="/history?entity_id={encodeURIComponent(row.entity_id)}" target="_top"
                  onclick={(event) => { if (row.entity_id && openMoreInfo(row.entity_id)) event.preventDefault(); }}>{row.target}</a>
              {:else}{row.target}{/if}
              {#if row.kind !== 'entity'} <span class="chip">{row.kind}</span>{/if}
            </td>
            <td>{row.name || ''}</td>
            <td>
              {#if !row.entity_id} <span class="muted">fetched when the task runs</span>
              {:else if entities.error}<span class="muted">-</span>
              {:else if row.missing}<span class="chip error">not found in Home Assistant</span>
              {:else if row.state == null || row.state === 'unknown' || row.state === 'unavailable'}
                <span class="chip warn">{String(row.state ?? 'no value')}</span>
              {:else}{value(row)}{/if}
            </td>
            <td><button class="secondary" disabled={!usable || !check?.entities || saving || editingEntities}
              onclick={() => changeEntities({ remove: row.alias, section: row.section })}>Remove</button></td>
          </tr>
        {/each}
      </tbody>
    </table></div>
  {:else}<p class="muted">{usable ? 'This task reads no entities, files or URLs.' : 'Fix the task YAML to see its entities.'}</p>{/if}
</div>
<h3>Add an entity</h3>
<input class="search" type="search" aria-label="Search entities" placeholder="Search by entity ID or name" bind:value={search}>
{#if searchError}<div class="banner error">{searchError}</div>{/if}
{#if search.trim().length >= 2}
  <div class="table-scroll"><table><tbody>
    {#each matches as entity (entity.entity_id)}
      <tr>
        <td class="mono">{entity.entity_id}</td><td>{entity.name}</td>
        <td>{String(entity.state ?? '')} {entity.unit || ''}</td>
        <td>
          {#if !entities?.list_format && !known(entity.entity_id)}
            <input class="search" aria-label="Alias for {entity.entity_id}"
              value={aliases[entity.entity_id] ?? defaultAlias(entity.entity_id)}
              oninput={(event) => aliases[entity.entity_id] = event.currentTarget.value}>
          {/if}
          <button class="secondary" disabled={!usable || !check?.entities || editingEntities || saving || known(entity.entity_id)}
            onclick={() => changeEntities({ add: { entity_id: entity.entity_id,
              alias: entities?.list_format ? '' : (aliases[entity.entity_id] ?? defaultAlias(entity.entity_id)).trim() } })}>
            {known(entity.entity_id) ? 'Added' : 'Add'}
          </button>
        </td>
      </tr>
    {/each}
  </tbody></table></div>
  {#if total > matches.length}<p class="muted">{total - matches.length} more - refine the search.</p>{/if}
{/if}
<p class="muted">Add/Remove edits the YAML only. Use {'{alias}'} in the prompt. Save writes the task.</p>
<div class="row">
  <h3>Task YAML</h3>
  <button class="secondary chip" class:ok={usable} class:error={!checking && check?.valid === false}
    onclick={() => goToLine(check?.line, check?.column)}>
    {checking ? 'Checking...' : check?.valid ? 'YAML OK' : check?.message || 'Check unavailable'}
  </button>
  <span class="grow"></span>
  <button class="secondary" disabled={!usable || previewBusy || editingEntities} onclick={runPreview}>
    {previewBusy ? 'Running...' : 'Preview'}
  </button>
  <button class="secondary" disabled={!dirty || saving || editingEntities} onclick={discard}>Discard</button>
  <button disabled={!dirty || !usable || saving || editingEntities || fileBusy} onclick={save}>
    {saving ? 'Saving...' : dirty ? 'Save *' : 'Save'}
  </button>
</div>
<p class="muted">The whole task, including entities, files, URLs and comments. Ctrl+S saves; Ctrl+Enter previews
  this unsaved text. Preview writes no memory, audit, target sensor or MQTT update.</p>
<textarea class="code" aria-label="Task YAML" wrap="soft" bind:this={editor} bind:value={text} onkeydown={keydown}></textarea>
{#each errors as problem}
  <div class="banner error" role="alert">
    {#if problem.line}<button class="secondary" onclick={() => goToLine(problem.line, problem.column)}>Line {problem.line}</button>{/if}
    {problem.message}
  </div>
{/each}
{#each warnings as warning}<div class="banner">{warning}</div>{/each}
{#if notice}<p role="status">{notice}</p>{/if}
{#if previewBusy}<h3>Preview</h3><p class="muted">Running the unsaved task...</p>{/if}
{#if preview}
  <div class="row">
    <h3>Preview</h3>
    <span class="chip" class:ok={preview.ok && !!preview.prompt} class:warn={preview.ok && !preview.prompt} class:error={!preview.ok}>
      {preview.ok ? preview.prompt ? 'OK' : 'No prompt' : 'Error'}
    </span>
    <span class="muted">{preview.duration ?? '-'}s - not saved, not published</span>
  </div>
  {#if text !== previewText}<div class="banner">The YAML has changed since this preview.</div>{/if}
  {#if preview.ok}
    {#if !preview.prompt}<div class="banner">No prompt, so no LLM was called. This data-only result contains the collected values.</div>{/if}
    <h4>Result</h4><ResultView result={preview.result} />
    {#if preview.attachments?.length}<p class="muted">Attachments: {preview.attachments.join(', ')}</p>{/if}
    {#if preview.prompt}<details><summary>Prompt as sent</summary><pre>{preview.prompt}</pre></details>{/if}
  {:else}
    <div class="banner error" role="alert">{preview.error}</div>
    {#if preview.traceback}<details><summary>Traceback</summary><pre>{preview.traceback}</pre></details>{/if}
  {/if}
{/if}
