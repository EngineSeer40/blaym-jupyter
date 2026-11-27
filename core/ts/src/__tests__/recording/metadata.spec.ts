import { applyPolicy } from '../../recording';
import { FIXTURES, POLICY } from './generate';

declare const __dirname: string;
declare const require: (name: string) => any;
const { readFileSync } = require('fs');
const { join } = require('path');

const cases = JSON.parse(
  readFileSync(join(FIXTURES, 'metadata-cases.json'), 'utf8')
).cases as any[];

describe('metadata policy applier (shared cases with Python)', () => {
  test('every shared case gives the same metadata and dropped keys', () => {
    expect(cases.length).toBeGreaterThanOrEqual(9);
    for (const c of cases) {
      const policy = { ...POLICY, ...(c.policyOverride ?? {}) };
      const input = JSON.parse(JSON.stringify(c.input));
      const out = applyPolicy(c.input, c.level, policy);
      expect(c.input).toEqual(input); // never mutated
      expect(out.metadata).toEqual(c.expected);
      expect(out.dropped).toEqual([...c.dropped].sort());
    }
  });
});

describe('the policy copy shipped in the TypeScript core', () => {
  test('equals the Python core file it is copied from', () => {
    const copy = JSON.parse(
      readFileSync(
        join(__dirname, '..', '..', 'recording', 'metadata-policy.json'),
        'utf8'
      )
    );
    expect(copy).toEqual(POLICY);
  });
});
