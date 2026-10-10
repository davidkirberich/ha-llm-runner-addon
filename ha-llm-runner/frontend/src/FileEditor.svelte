<script lang="ts">
  import { onMount, tick, untrack } from 'svelte';
  import { api, ApiError } from './api';
  import { applyEdit, indentEdit, lineOffset } from './editor';

  let { kind, blocked, active, onbusy, ondirty, onsaved }: {
    kind: 'config' | 'processors';
    blocked: boolean;
    active: boolean;
    onbusy: (value: boolean) => void;
    ondirty: (value: boolean) => void;
    onsaved: () => void;
  } = $props();
  let items = $state<{ name: string; used_by: string[] }[]>([]);
  let name = $state('');
  let folder = $state('');
  let text = $state('');
  let baseline = $state('');
  let isNew = $state(false);
  let dirty = $derived(isNew || text !== baseline);
  let busy = $state(false);
  let errors = $state<{ message: string; line?: number; column?: number }[]>([]);
  let warnings = $state<string[]>([]);
  let notice = $state('');
  let usedBy = $state<string[]>([]);
  let creating = $state(false);
  let newName = $state('my_processor.py');
  let editor = $state<HTMLTextAreaElement>();
  let cursor = $state({ line: 1, column: 1 });
  const lifetime = new AbortController();
  let disposed = false;
  let sequence = 0;
  const endpoint = $derived(kind === 'config' ? 'config' : `processors/${encodeURIComponent(name)}`);

  function report(reason: unknown) {
    if (disposed) return;
    const data = reason instanceof ApiError ? reason.data : {};
    errors = data.errors || [{ message: reason instanceof Error ? reason.message : String(reason), line: data.line, column: data.column }];
    warnings = data.warnings || [];
  }
  async function list() {
    const data = await api<{ folder: string; processors: typeof items }>('GET', 'processors', lifetime.signal);
    if (!disposed) { folder = data.folder; items = data.processors; }
  }
  async function load(wanted = name) {
    if (busy || (dirty && !confirm('Discard your unsaved changes?'))) return;
    const token = ++sequence;
    busy = true;
    try {
      const data = await api<{ content: string; path?: string; used_by?: string[] }>('GET',
        kind === 'config' ? 'config' : `processors/${encodeURIComponent(wanted)}`, lifetime.signal);
      if (disposed || token !== sequence) return;
      name = wanted;
      text = baseline = kind === 'config' && !data.content ? 'tasks:\n' : data.content;
      folder = data.path || folder;
      usedBy = data.used_by || [];
      isNew = false;
      errors = []; warnings = []; notice = '';
    } catch (reason) { report(reason); }
    finally { if (!disposed) busy = false; }
  }
  function create() {
    if (busy || (dirty && !confirm('Discard your unsaved changes?'))) return;
    let wanted = newName.trim();
    if (!wanted) return;
    if (!wanted.endsWith('.py')) wanted += '.py';
    if (items.some((item) => item.name === wanted)) {
      errors = [{ message: 'A processor with this name already exists. Open it from the list.' }];
      return;
    }
    name = wanted;
    creating = false;
    baseline = '';
    text = 'import pandas as pd\n\n\ndef process(df: pd.DataFrame, config: dict):\n'
      + '    """Returns (values for the prompt, data for {data})."""\n'
      + '    current = {column: round(float(df[column].dropna().iloc[-1]), 2) for column in df.columns if not df[column].dropna().empty}\n'
      + '    hourly = df.resample(config.get("resample", "1h")).mean().round(2)\n'
      + '    return current, hourly.reset_index().to_json(orient="records", date_format="iso")\n';
    isNew = true;
    usedBy = []; errors = []; warnings = []; notice = '';
  }
  async function perform(save: boolean) {
    if (busy || (save && blocked) || (kind === 'processors' && !name)) return;
    busy = true;
    const content = text;
    errors = []; warnings = []; notice = '';
    try {
      const data = await api<{ valid?: boolean; errors?: typeof errors; warnings?: string[]; task_ids?: string[]; mqtt_synced?: boolean }>(
        save ? 'PUT' : 'POST', save ? endpoint : `${endpoint}/validate`, lifetime.signal, { content });
      if (disposed) return;
      if (save) {
        baseline = content;
        isNew = false;
        notice = text === content ? 'Saved' : 'Snapshot saved; newer editor changes are still unsaved.';
        if (kind === 'config') notice += data.mqtt_synced ? ' - Home Assistant entities updated' : ' - entities update once MQTT connects';
        warnings = data.warnings || [];
        if (kind === 'processors') await list();
        onsaved();
      } else {
        errors = data.errors || [];
        warnings = data.warnings || [];
        notice = data.valid
          ? kind === 'processors'
            ? 'Valid - compiles, has process(df, config) and all imports are installed'
            : `Valid - ${data.task_ids?.length ?? 0} task(s)`
          : 'Validation failed';
        if (text !== content) notice += ' (editor changed since validation)';
      }
    } catch (reason) { report(reason); }
    finally { if (!disposed) busy = false; }
  }
  async function remove() {
    if (busy || blocked || !name || !confirm(`Delete ${name}?${dirty ? ' Unsaved changes will be lost.' : ''}`)) return;
    busy = true;
    try {
      await api('DELETE', endpoint, lifetime.signal);
      if (disposed) return;
      name = ''; text = baseline = ''; isNew = false;
      await list();
      notice = 'Deleted'; errors = []; warnings = [];
      onsaved();
    } catch (reason) { report(reason); }
    finally { if (!disposed) busy = false; }
  }
  function jump(line?: number, column?: number) {
    if (!line || !editor) return;
    editor.focus();
    const offset = lineOffset(text, line, column);
    editor.setSelectionRange(offset, offset);
    editor.scrollTop = Math.max(0, (line - 5) * 18);
    updateCursor();
  }
  function updateCursor() {
    if (!editor) return;
    const before = editor.value.slice(0, editor.selectionStart);
    cursor = { line: before.split('\n').length, column: before.length - before.lastIndexOf('\n') };
  }
  async function keydown(event: KeyboardEvent) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault(); void perform(true);
    } else if (event.key === 'Tab' && !event.ctrlKey && !event.altKey && !event.metaKey) {
      event.preventDefault();
      if (!editor) return;
      const edit = indentEdit(text, editor.selectionStart, editor.selectionEnd, kind === 'config' ? '  ' : '    ', event.shiftKey);
      text = applyEdit(editor, edit);
      await tick(); editor?.setSelectionRange(edit.selectionStart, edit.selectionEnd); updateCursor();
    }
  }
  $effect(() => { ondirty(dirty); });
  $effect(() => { onbusy(busy); });
  $effect(() => {
    if (active) untrack(() => {
      void (kind === 'config' ? (!dirty ? load() : Promise.resolve()) : list()).catch(report);
    });
  });
  onMount(() => {
    return () => { disposed = true; lifetime.abort(); ondirty(false); onbusy(false); };
  });
</script>

<section class="card">
  <div class="row">
    <h2>{kind === 'config' ? 'llm_tasks.yaml' : 'Processors'}</h2>
    <span class="muted">{folder}</span><span class="grow"></span>
    {#if kind === 'processors'}<button class="secondary" disabled={busy} onclick={() => creating = true}>New</button>{/if}
    <button class="secondary" disabled={busy} onclick={() => kind === 'config' ? load() : list().catch(report)}>Reload</button>
  </div>
  {#if creating}
    <form class="row" onsubmit={(event) => { event.preventDefault(); create(); }}>
      <label>Processor file name <input class="search" required pattern={'[A-Za-z0-9_\\-]+(\\.py)?'} bind:value={newName}></label>
      <button type="submit">Create processor</button>
      <button type="button" class="secondary" onclick={() => creating = false}>Cancel</button>
    </form>
  {/if}
  <div class:processor-layout={kind === 'processors'}>
  {#if kind === 'processors'}
    <div class="processor-list" role="group" aria-label="Processors">
      {#each items as item (item.name)}
        <button class="secondary" class:active={name === item.name} disabled={busy} onclick={() => load(item.name)}>
          {item.name} <span class="muted">({item.used_by.length} {item.used_by.length === 1 ? 'task' : 'tasks'})</span>
        </button>
      {:else}<p class="muted">No processors yet.</p>{/each}
    </div>
  {/if}
  <div class="file-editor">
  {#if kind === 'config' || name}
    <div class="row">
      <h3>{kind === 'config' ? 'Configuration' : name}</h3>
      {#if kind === 'processors'}<span class="muted">{usedBy.length ? `used by ${usedBy.join(', ')}` : 'not used by any task'}</span>{/if}
      <span class="grow"></span>
      <span class="muted">Line {cursor.line}, column {cursor.column}</span>
      <button class="secondary" disabled={busy} onclick={() => perform(false)}>Validate</button>
      {#if notice}<span role="status">{notice}</span>{/if}
      <button disabled={busy || blocked} onclick={() => perform(true)}>{busy ? 'Working...' : dirty ? 'Save *' : 'Save'}</button>
      {#if kind === 'processors'}<button class="danger" disabled={busy || isNew || blocked} onclick={remove}>Delete</button>{/if}
    </div>
  {:else}
    <p class="muted">Select a processor from the list or create a new one.</p>
  {/if}
  {#if blocked}<p class="muted">Save or discard the changes in the other editor first.</p>{/if}
  {#each errors as error}
    <div class="banner error" role="alert">
      {#if error.line}<button class="secondary" onclick={() => jump(error.line, error.column)}>Line {error.line}</button>{/if}
      {error.message}
    </div>
  {/each}
  {#each warnings as warning}<div class="banner">{warning}</div>{/each}
  {#if kind === 'config' || name}
    <p class="muted">Ctrl+S saves. {kind === 'config' ? 'Saving validates the YAML and keeps a backup.' : 'Validate compiles without running the processor; saved changes apply on the next run.'}</p>
    <textarea class="code" aria-label={kind === 'config' ? 'Configuration YAML' : 'Processor code'}
      bind:this={editor} bind:value={text} onkeydown={keydown} oninput={updateCursor}
      onkeyup={updateCursor} onclick={updateCursor} onselect={updateCursor} disabled={busy && !dirty}></textarea>
  {:else if notice}<p role="status">{notice}</p>{/if}
  </div>
  </div>
</section>
