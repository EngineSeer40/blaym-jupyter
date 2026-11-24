import { applyPolicy, utf8Length } from '../../recording';
import { FIXTURES, POLICY } from './generate';

declare const require: (name: string) => any;
const { readFileSync } = require('fs');
const { join } = require('path');

const cases = JSON.parse(readFileSync(join(FIXTURES, 'metadata-cases.json'), 'utf8')).cases as any[];

describe('metadata policy applier (shared cases with Python)', () => {
  test('the shared cases file has cases', () => {
    expect(cases.length).toBeGreaterThanOrEqual(9);
  });

  test.each(cases.map(c => [c.name, c]))('%s', (_name, c: any) => {
    const policy = { ...POLICY, ...(c.policyOverride ?? {}) };
    const input = JSON.parse(JSON.stringify(c.input));
    const out = applyPolicy(c.input, c.level, policy);
    expect(c.input).toEqual(input); // never mutated
    expect(out.metadata).toEqual(c.expected);
    expect(out.dropped).toEqual([...c.dropped].sort());
  });

  test('size is UTF-8 bytes of compact JSON, top-level values only', () => {
    expect(utf8Length('é€😀a')).toBe(2 + 3 + 4 + 1);
    const big = 'x'.repeat(POLICY.maxValueBytes);
    const out = applyPolicy({ big, small: 1, nested: { inner: big.slice(0, 10) } }, 'notebook', POLICY);
    expect(out.metadata.big).toEqual({ blaymDropped: 'too_large', bytes: POLICY.maxValueBytes + 2 });
    expect(out.metadata.small).toBe(1);
    expect(out.dropped).toEqual(['big']);
  });

  test('an emptied execution is removed, a non-object execution is kept', () => {
    const t = { 'iopub.execute_input': 'a', 'shell.execute_reply': 'b' };
    expect(applyPolicy({ execution: t }, 'cell', POLICY).metadata).toEqual({});
    expect(applyPolicy({ execution: 'odd' }, 'cell', POLICY).metadata).toEqual({ execution: 'odd' });
  });
});
