import * as fc from 'fast-check';
import { checkOutline, isSettledAt } from '../../navigation';
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
  it('generated outlines with run spans are sound and not all empty', () => {
    let withSpans = 0;
    fc.assert(
      fc.property(opsArb, ops => {
        const o = outlineOfHistory(buildHistory(ops));
        expect(checkOutline(o)).toEqual([]);
        if (o.branches.some(b => b.runSpans.length > 0)) {
          withSpans += 1;
        }
      }),
      { numRuns: 300 }
    );
    expect(withSpans).toBeGreaterThan(30);
  });

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

describe('isSettledAt at a tick that is not on the line', () => {
  // Guards a gate defect: isSettledAt once asked whether a span is open at the
  // asked tick, but a tick off the line shows the state at the last event on the line
  // at or before it. Here b1 branches from tick 1, so tick 2 (a run start on b0) is not
  // on b1's line, and b1 at tick 2 shows tick 1: settled.
  it('uses the last event on the line, not the asked tick', () => {
    const h = buildHistory([
      { kind: 'event', sub: 0, pick: 0, at: 0 },
      { kind: 'branch', sub: 0, pick: 0, at: 0 }
    ]);
    const o = outlineOfHistory(h);
    expect(openRunsAt(h, 'b1', 2)).toEqual([]);
    expect(isSettledAt(o, 'b1', 2)).toBe(true);
  });
});

describe('isSettledAt agrees with the rebuilder in every fixture', () => {
  const DIR: string = join(__dirname, '..', '..', '..', 'fixtures', 'rebuild');
  const files: string[] = readdirSync(DIR)
    .filter((f: string) => f.endsWith('.json'))
    .sort();
  let total = 0;
  let unsettled = 0;
  let withRuns = 0;

  it.each(files.map(f => [f]))('%s', (file: string) => {
    const data = JSON.parse(readFileSync(join(DIR, file), 'utf8'));
    for (const ex of data.expectations) {
      if (ex.result.status !== 'ok') {
        continue;
      }
      const runs: { status: string }[] = ex.result.state.runs;
      const running = runs.some(r => r.status === 'running');
      expect(isSettledAt(data.outline, ex.branchId, ex.tick)).toBe(!running);
      total += 1;
      if (runs.length) {
        withRuns += 1;
      }
      if (running) {
        unsettled += 1;
      }
    }
  });

  it('exercises both settled and unsettled moments', () => {
    // eslint-disable-next-line no-console
    console.log(
      `fixture positions ${total}, with runs ${withRuns}, running ${unsettled}`
    );
    expect(unsettled).toBeGreaterThan(0);
    expect(total - unsettled).toBeGreaterThan(0);
  });
});
