export function defaultAlias(entityId: string): string {
  return entityId.split('.').slice(1).join('.').replace(/[^A-Za-z0-9_]/g, '_').replace(/^(\d+)$/, '_$1');
}

export function lineOffset(text: string, line: number, column = 1): number {
  const lines = text.split('\n');
  const index = Math.max(0, Math.min(line - 1, lines.length - 1));
  return lines.slice(0, index).reduce((offset, value) => offset + value.length + 1, 0)
    + Math.max(0, Math.min(column - 1, lines[index].length));
}

export function insertIndent(text: string, start: number, end: number) {
  return { text: text.slice(0, start) + '  ' + text.slice(end), cursor: start + 2 };
}

export function openMoreInfo(entityId: string): boolean {
  try {
    const ha = window.parent !== window && window.parent.document.querySelector('home-assistant');
    if (!ha) return false;
    ha.dispatchEvent(new CustomEvent('hass-more-info', {
      detail: { entityId }, bubbles: true, composed: true,
    }));
    return true;
  } catch (error) {
    if (error instanceof DOMException && error.name === 'SecurityError') return false;
    throw error;
  }
}
