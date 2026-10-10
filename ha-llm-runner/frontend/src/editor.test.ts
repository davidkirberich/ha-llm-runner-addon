import { expect, it } from 'vitest';
import { defaultAlias, insertIndent, lineOffset } from './editor';

it('preserves supported aliases including leading digits', () => {
  expect(defaultAlias('sensor.1OG_temperature')).toBe('1OG_temperature');
  expect(defaultAlias('sensor.123')).toBe('_123');
  expect(defaultAlias('sensor.some-name')).toBe('some_name');
});

it('inserts indentation at the selection', () => {
  expect(insertIndent('abc', 1, 2)).toEqual({ text: 'a  c', cursor: 3 });
});

it('finds YAML error positions and clamps invalid positions', () => {
  expect(lineOffset('a:\n  b: x', 2, 3)).toBe(5);
  expect(lineOffset('a:\n  b: x', 99, 99)).toBe(9);
  expect(lineOffset('', 1, 1)).toBe(0);
});
