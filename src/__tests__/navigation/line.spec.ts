import * as fc from 'fast-check';
import type { HistoryOutline, OutlineBranch } from 'blaym-jupyter-types';
import {
  branchLine,
  checkOutline,
  isSettledAt,
  lineTicks,
  nextTick,
  ownerOfTick,
  positionAt,
  previousTick,
  sharedPart,
  tipOf
} from '../../navigation';

// Node's own modules, declared here because the project does not depend on @types/node.
declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');

function outlineOf(branches: OutlineBranch[]): HistoryOutline {
  const all: number[] = [];
  branches.forEach(b => b.ownTicks.forEach(t => all.push(t)));
  return {
    schemaVersion: 2,
    explorationId: 'x',
    rootBranchId: branches[0].branchId,
    highestTick: Math.max(...all),
    branches
  };
}

function branch(
  id: string,
  ownTicks: number[],
  parent?: [string, number],
  runSpans: OutlineBranch['runSpans'] = []
): OutlineBranch {
  return {
    branchId: id,
    name: id,
    ownTicks,
    runSpans,
    ...(parent ? { parentBranchId: parent[0], parentTick: parent[1] } : {})
  };
}

// main: 1 2 3 8, try branches after 2: 4 5, deep branches from try at 4: 6 7
const TREE = outlineOf([
  branch('main', [1, 2, 3, 8]),
  branch('try', [4, 5], ['main', 2]),
  branch('deep', [6, 7], ['try', 4])
]);

describe('line arithmetic, named cases', () => {
  it('builds a line from ancestors up to each branch point', () => {
    expect(lineTicks(TREE, 'main')).toEqual([1, 2, 3, 8]);
    expect(lineTicks(TREE, 'try')).toEqual([1, 2, 4, 5]);
    expect(lineTicks(TREE, 'deep')).toEqual([1, 2, 4, 6, 7]);
    expect(lineTicks(TREE, 'nope')).toEqual([]);
  });

  it('steps back and forward, and stops at the ends', () => {
    expect(previousTick(TREE, 'deep', 6)).toBe(4);
    expect(previousTick(TREE, 'deep', 1)).toBeUndefined();
    expect(nextTick(TREE, 'deep', 4)).toBe(6);
    expect(nextTick(TREE, 'deep', 7)).toBeUndefined();
    expect(tipOf(TREE, 'try')).toBe(5);
    expect(tipOf(TREE, 'nope')).toBeUndefined();
  });

  it('works out branch count and last event tick', () => {
    expect(positionAt(TREE, 'deep', 7)).toEqual({
      branchId: 'deep',
      tick: 7,
      lastEventTick: 7,
      branchCount: 5
    });
    // asked tick between events, and on another branch's tick
    expect(positionAt(TREE, 'deep', 5)).toEqual({
      branchId: 'deep',
      tick: 5,
      lastEventTick: 4,
      branchCount: 3
    });
    expect(positionAt(TREE, 'deep', 0)).toBeUndefined();
  });

  it('finds the shared part and the owner of a tick', () => {
    expect(sharedPart(TREE, 'try', 'deep').map(e => e.tick)).toEqual([1, 2, 4]);
    expect(sharedPart(TREE, 'main', 'try').map(e => e.tick)).toEqual([1, 2]);
    expect(ownerOfTick(TREE, 'deep', 2)).toBe('main');
    expect(ownerOfTick(TREE, 'deep', 5)).toBe('try');
    expect(ownerOfTick(TREE, 'deep', 7)).toBe('deep');
    expect(ownerOfTick(TREE, 'deep', 0)).toBeUndefined();
  });

  it('uses the spans of the branch that owns the tick (Task 2 gate)', () => {
    // main starts a run at 2, a kernel restart closes it at 4; child branches from 5.
    const o = outlineOf([
      branch('main', [1, 2, 3, 4, 5, 9], undefined, [
        { runId: 'r', startedTick: 2, endedTick: 4 }
      ]),
      // the run was closed before the branch point, so the child lists no span
      branch('child', [6, 7], ['main', 5])
    ]);
    expect(isSettledAt(o, 'child', 3)).toBe(false); // inside the run
    expect(isSettledAt(o, 'child', 2)).toBe(false);
    expect(isSettledAt(o, 'child', 4)).toBe(true); // closing event
    expect(isSettledAt(o, 'child', 5)).toBe(true);
    expect(isSettledAt(o, 'child', 7)).toBe(true);
    expect(isSettledAt(o, 'child', 1)).toBe(true);
    expect(isSettledAt(o, 'child', 0)).toBe(true);
  });

  it('treats a run open at the branch point as open on the child', () => {
    const o = outlineOf([
      branch('main', [1, 2, 3, 9], undefined, [{ runId: 'r', startedTick: 2 }]),
      branch(
        'child',
        [4, 5, 6],
        ['main', 3],
        [{ runId: 'r', startedTick: 2, endedTick: 5 }]
      )
    ]);
    expect(isSettledAt(o, 'child', 4)).toBe(false);
    expect(isSettledAt(o, 'child', 5)).toBe(true);
    expect(isSettledAt(o, 'main', 9)).toBe(false);
  });
});

describe('checkOutline', () => {
  it('accepts a sound outline', () => {
    expect(checkOutline(TREE)).toEqual([]);
  });

  const kinds = (o: HistoryOutline) => checkOutline(o).map(p => p.kind);

  it('reports each rule the schema cannot express', () => {
    expect(kinds(outlineOf([branch('m', [2, 1])]))).toContain(
      'unsorted_own_ticks'
    );
    expect(kinds(outlineOf([branch('m', [1, 1])]))).toContain(
      'repeated_own_ticks'
    );
    expect(kinds(outlineOf([branch('m', [1]), branch('c', [2])]))).toContain(
      'missing_parent'
    );
    // only one of parentBranchId / parentTick present
    const noTick = branch('c', [2], ['m', 1]);
    delete noTick.parentTick;
    expect(kinds(outlineOf([branch('m', [1]), noTick]))).toContain(
      'missing_parent'
    );
    const noParent = branch('c', [2], ['m', 1]);
    delete noParent.parentBranchId;
    expect(kinds(outlineOf([branch('m', [1]), noParent]))).toContain(
      'missing_parent'
    );
    expect(
      kinds(outlineOf([branch('m', [1]), branch('c', [2], ['zz', 1])]))
    ).toContain('unknown_parent');
    expect(
      kinds(
        outlineOf([
          branch('m', [1], undefined, [
            { runId: 'r', startedTick: 5, endedTick: 3 }
          ])
        ])
      )
    ).toContain('run_span_backwards');
    expect(
      kinds(outlineOf([branch('m', [1, 2]), branch('c', [2], ['m', 1])]))
    ).toContain('tick_claimed_twice');
    expect(
      kinds(
        outlineOf([
          branch('m', [1]),
          branch('a', [2], ['b', 1]),
          branch('b', [3], ['a', 2])
        ])
      )
    ).toContain('parent_loop');
    expect(
      kinds({ ...outlineOf([branch('m', [1])]), rootBranchId: 'q' })
    ).toContain('unknown_root');
    expect(kinds(outlineOf([branch('m', [1], ['m', 1])]))).toContain(
      'root_has_parent'
    );
    expect(kinds(outlineOf([branch('m', [1]), branch('m', [2])]))).toContain(
      'duplicate_branch'
    );
  });
});

// ---- generated outlines ---------------------------------------------------

type Op = { kind: 'event' | 'branch'; pick: number; at: number };

const opsArb = fc.array(
  fc.record({
    kind: fc.constantFrom<'event' | 'branch'>('event', 'event', 'branch'),
    pick: fc.nat(1000),
    at: fc.nat(1000)
  }),
  { minLength: 1, maxLength: 60 }
);

/** Builds a sound outline: events on any branch, branches cut from any tick on a line. */
function buildOutline(ops: Op[]): HistoryOutline {
  const branches: OutlineBranch[] = [branch('b0', [1])];
  let tick = 1;
  for (const op of ops) {
    const target = branches[op.pick % branches.length];
    if (op.kind === 'event') {
      tick += 1;
      target.ownTicks.push(tick);
    } else {
      const line = lineTicks(outlineOf(branches), target.branchId);
      const parentTick = line[op.at % line.length];
      tick += 1;
      branches.push(
        branch(`b${branches.length}`, [tick], [target.branchId, parentTick])
      );
    }
  }
  return outlineOf(branches);
}

describe('line arithmetic, generated outlines', () => {
  it('generated outlines are sound', () => {
    fc.assert(
      fc.property(opsArb, ops => {
        expect(checkOutline(buildOutline(ops))).toEqual([]);
      }),
      { numRuns: 300 }
    );
  });

  it('stepping forward then back returns to the same tick', () => {
    fc.assert(
      fc.property(opsArb, ops => {
        const o = buildOutline(ops);
        for (const b of o.branches) {
          const ticks = lineTicks(o, b.branchId);
          ticks.forEach((t, i) => {
            const n = nextTick(o, b.branchId, t);
            if (i === ticks.length - 1) {
              expect(n).toBeUndefined();
            } else {
              expect(n).toBe(ticks[i + 1]);
              expect(previousTick(o, b.branchId, n as number)).toBe(t);
            }
            if (i === 0) {
              expect(previousTick(o, b.branchId, t)).toBeUndefined();
            }
          });
        }
      }),
      { numRuns: 300 }
    );
  });

  it('a line starts with its parent line up to the branch point', () => {
    fc.assert(
      fc.property(opsArb, ops => {
        const o = buildOutline(ops);
        for (const b of o.branches) {
          if (b.parentBranchId === undefined) {
            continue;
          }
          const parent = lineTicks(o, b.parentBranchId).filter(
            t => t <= (b.parentTick as number)
          );
          const own = lineTicks(o, b.branchId);
          expect(own.slice(0, parent.length)).toEqual(parent);
          expect(own.slice(parent.length)).toEqual(b.ownTicks);
        }
      }),
      { numRuns: 300 }
    );
  });

  it('every tick on a line belongs to the branch or an ancestor; the tip is the last tick', () => {
    fc.assert(
      fc.property(opsArb, ops => {
        const o = buildOutline(ops);
        for (const b of o.branches) {
          const ancestors = new Set<string>([b.branchId]);
          let cur: OutlineBranch | undefined = b;
          while (cur && cur.parentBranchId !== undefined) {
            ancestors.add(cur.parentBranchId);
            const pid: string = cur.parentBranchId;
            cur = o.branches.find(x => x.branchId === pid);
          }
          const line = branchLine(o, b.branchId);
          line.forEach(e => expect(ancestors.has(e.branchId)).toBe(true));
          const ticks = line.map(e => e.tick);
          expect(ticks).toEqual([...ticks].sort((x, y) => x - y));
          expect(tipOf(o, b.branchId)).toBe(ticks[ticks.length - 1]);
          // count is the index along the line
          ticks.forEach((t, i) =>
            expect(positionAt(o, b.branchId, t)?.branchCount).toBe(i + 1)
          );
        }
      }),
      { numRuns: 300 }
    );
  });
});

// ---- the cross-language check --------------------------------------------

const DIR: string = join(__dirname, '..', '..', '..', 'fixtures', 'rebuild');
const files: string[] = readdirSync(DIR)
  .filter((f: string) => f.endsWith('.json'))
  .sort();

describe('Python and TypeScript agree on positions', () => {
  it.each(files.map(f => [f]))('%s', (file: string) => {
    const data = JSON.parse(readFileSync(join(DIR, file), 'utf8'));
    expect(checkOutline(data.outline)).toEqual([]);
    let checked = 0;
    for (const ex of data.expectations) {
      if (ex.result.status !== 'ok') {
        continue;
      }
      const expected = ex.result.state.position;
      const got = positionAt(data.outline, ex.branchId, ex.tick);
      expect(got).toBeDefined();
      expect(got).toEqual(expected);
      checked += 1;
    }
    expect(checked).toBeGreaterThan(0);
  });
});
