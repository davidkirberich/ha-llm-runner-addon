import { expect, it } from 'vitest';
import { defaultAlias, indentEdit, lineOffset } from './editor';

function apply(text: string, edit: ReturnType<typeof indentEdit>) {
  return text.slice(0, edit.from) + edit.insert + text.slice(edit.to);
}

it('preserves supported aliases including leading digits', () => {
  expect(defaultAlias('sensor.1OG_temperature')).toBe('1OG_temperature');
  expect(defaultAlias('sensor.123')).toBe('_123');
  expect(defaultAlias('sensor.some-name')).toBe('some_name');
});

it('inserts indentation at a plain cursor', () => {
  const edit = indentEdit('abc', 1, 1, '  ');
  expect(apply('abc', edit)).toBe('a  bc');
  expect([edit.selectionStart, edit.selectionEnd]).toEqual([3, 3]);
});

it('indents every selected line without replacing the selection', () => {
  const text = 'a:\n  b: 1\n\nc: 2';
  const edit = indentEdit(text, 1, text.length, '  ');
  expect(apply(text, edit)).toBe('  a:\n    b: 1\n\n  c: 2');
  expect([edit.selectionStart, edit.selectionEnd]).toEqual([3, text.length + 6]);
});

it('does not indent the line after a selection ending in a newline', () => {
  const text = 'a\nb\nc';
  expect(apply(text, indentEdit(text, 0, 4, '  '))).toBe('  a\n  b\nc');
});

it('outdents spaces and tabs', () => {
  const text = '    a\n\tb\n c\nd';
  const edit = indentEdit(text, 0, text.length, '  ', true);
  expect(apply(text, edit)).toBe('  a\nb\nc\nd');
  expect(edit.selectionStart).toBe(0);
  expect(apply('  a', indentEdit('  a', 3, 3, '  ', true))).toBe('a');
});

it('finds YAML error positions and clamps invalid positions', () => {
  expect(lineOffset('a:\n  b: x', 2, 3)).toBe(5);
  expect(lineOffset('a:\n  b: x', 99, 99)).toBe(9);
  expect(lineOffset('', 1, 1)).toBe(0);
});
