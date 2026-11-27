import * as fc from 'fast-check';
import { isSettledAt } from '../../navigation';
import {
  buildHistory,
  lineEvents,
  openRunsAt,
  opsArb,
  outlineOfHistory
} from './outline-gen';

// Node's own modules, declared here because the project does not depend on @types/node.
declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');

describe('isSettledAt against a simple independent model', () => {
  it('matches the model at every tick on the line of every branch', () => {
    let open = 0;
    let checked = 0;
    fc.assert(
      fc.property(opsArb, ops => {
        const h = buildHistory(ops);
        const o = outlineOfHistory(h);
        for (const b of h.branches) {
          const ticks = [0].concat(lineEvents(h, b.branchId).map(e => e.tick));
          for (const t of ticks) {
            const model = openRunsAt(h, b.branchId, t).length === 0;
            expect(isSettledAt(o, b.branchId, t)).toBe(model);
            checked += 1;
            if (!model) {
              open += 1;
            }
          }
        }
      }),
      { numRuns: 400 }
    );
    expect(open).toBeGreaterThan(500);
    expect(checked).toBeGreaterThan(open);
  });
});

describe('isSettledAt agrees with the rebuilder in every fixture', () => {
  const DIR: string = join(__dirname, '..', '..', '..', 'fixtures', 'rebuild');
  const files: string[] = readdirSync(DIR)
    .filter((f: string) => f.endsWith('.json'))
    .sort();

  it('every fixture position is settled exactly when the rebuilder shows no running run', () => {
    let unsettled = 0;
    let total = 0;
    for (const file of files) {
      const data = JSON.parse(readFileSync(join(DIR, file), 'utf8'));
      for (const ex of data.expectations) {
        if (ex.result.status !== 'ok') {
          continue;
        }
        const running = ex.result.state.runs.some(
          (r: { status: string }) => r.status === 'running'
        );
        expect(isSettledAt(data.outline, ex.branchId, ex.tick)).toBe(!running);
        total += 1;
        unsettled += running ? 1 : 0;
      }
    }
    expect(unsettled).toBeGreaterThan(0);
    expect(total - unsettled).toBeGreaterThan(0);
  });
});
