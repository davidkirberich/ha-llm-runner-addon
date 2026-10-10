<script lang="ts">
  let { result }: { result: unknown } = $props();
  const text = $derived(
    result !== null && typeof result === 'object' && 'text' in result && typeof result.text === 'string'
      ? result.text : null,
  );
  const pretty = $derived(typeof result === 'string' ? result : JSON.stringify(result, null, 2));
</script>

{#if text !== null}
  <pre>{text}</pre>
  <details>
    <summary>Raw result (as published to Home Assistant)</summary>
    <pre>{pretty}</pre>
  </details>
{:else}
  <pre>{pretty}</pre>
{/if}
