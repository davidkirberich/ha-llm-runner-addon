export function defaultAlias(entityId: string): string {
  return entityId.split('.').slice(1).join('.').replace(/[^A-Za-z0-9_]/g, '_').replace(/^(\d+)$/, '_$1');
}

export function lineOffset(text: string, line: number, column = 1): number {
  const lines = text.split('\n');
  const index = Math.max(0, Math.min(line - 1, lines.length - 1));
  return lines.slice(0, index).reduce((offset, value) => offset + value.length + 1, 0)
    + Math.max(0, Math.min(column - 1, lines[index].length));
}

export interface TextEdit {
  from: number;
  to: number;
  insert: string;
  selectionStart: number;
  selectionEnd: number;
}

/** Tab indents (Shift+Tab outdents) every line touched by the selection; a plain Tab inserts at the cursor. */
export function indentEdit(text: string, start: number, end: number, unit: string, outdent = false): TextEdit {
  if (start === end && !outdent) {
    return { from: start, to: end, insert: unit, selectionStart: start + unit.length, selectionEnd: start + unit.length };
  }
  const from = text.lastIndexOf('\n', start - 1) + 1;
  const last = end > start && text[end - 1] === '\n' ? end - 1 : end;
  const lineEnd = text.indexOf('\n', last);
  const to = lineEnd === -1 ? text.length : lineEnd;
  const lines = text.slice(from, to).split('\n');
  const changes = lines.map((line) => {
    if (!outdent) return line ? unit.length : 0;
    const match = /^(\t| +)/.exec(line);
    return match ? -(match[1] === '\t' ? 1 : Math.min(match[1].length, unit.length)) : 0;
  });
  const insert = lines.map((line, index) => changes[index] > 0 ? unit + line : line.slice(-changes[index])).join('\n');
  const total = changes.reduce((sum, value) => sum + value, 0);
  const selectionStart = Math.max(from, start + changes[0]);
  return { from, to, insert, selectionStart, selectionEnd: Math.max(selectionStart, end + total) };
}

/** Applies an edit through the browser's editing commands, so Ctrl+Z can undo it. Returns the new text. */
export function applyEdit(editor: HTMLTextAreaElement, edit: TextEdit): string {
  if (editor.value.slice(edit.from, edit.to) !== edit.insert) {
    editor.focus();
    editor.setSelectionRange(edit.from, edit.to);
    const applied = typeof document.execCommand === 'function'
      && document.execCommand(edit.insert ? 'insertText' : 'delete', false, edit.insert);
    if (!applied || editor.value.slice(edit.from, edit.from + edit.insert.length) !== edit.insert) {
      editor.setRangeText(edit.insert, edit.from, edit.to);
    }
  }
  editor.setSelectionRange(edit.selectionStart, edit.selectionEnd);
  return editor.value;
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
