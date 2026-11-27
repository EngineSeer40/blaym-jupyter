import type { HistoryOutline, OutlineBranch } from 'blaym-notebook-types';
import {
  checkOutline,
  isSettledAt,
  lineTicks,
  nextTick,
  positionAt,
  previousTick,
  tipOf
} from '../../navigation';

// Node's own modules, declared here because the project does not depend on @types/node.
declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');

const DIR: string = join(__dirname, '..', '..', '..', '..', '..', 'fixtures', 'rebuild');
const files: string[] = readdirSync(DIR)
  .filter((f: string) => f.endsWith('.json'))
  .sort();

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
  it('builds a line from ancestors, and steps along it, stopping at the ends', () => {
    expect(lineTicks(TREE, 'main')).toEqual([1, 2, 3, 8]);
    expect(lineTicks(TREE, 'try')).toEqual([1, 2, 4, 5]);
    expect(lineTicks(TREE, 'deep')).toEqual([1, 2, 4, 6, 7]);
    expect(lineTicks(TREE, 'nope')).toEqual([]);

    expect(previousTick(TREE, 'deep', 6)).toBe(4);
    expect(previousTick(TREE, 'deep', 1)).toBeUndefined();
    expect(nextTick(TREE, 'deep', 4)).toBe(6);
    expect(nextTick(TREE, 'deep', 7)).toBeUndefined();
    expect(tipOf(TREE, 'try')).toBe(5);
    expect(tipOf(TREE, 'nope')).toBeUndefined();
  });

  it('settledness uses the spans of the branch that owns the tick, and a run open at the branch point is open on the child', () => {
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
    const o2 = outlineOf([
      branch('main', [1, 2, 3, 9], undefined, [{ runId: 'r', startedTick: 2 }]),
      branch(
        'child',
        [4, 5, 6],
        ['main', 3],
        [{ runId: 'r', startedTick: 2, endedTick: 5 }]
      )
    ]);
    expect(isSettledAt(o2, 'child', 4)).toBe(false);
    expect(isSettledAt(o2, 'child', 5)).toBe(true);
    expect(isSettledAt(o2, 'main', 9)).toBe(false);
  });
});

describe('checkOutline', () => {
  const kinds = (o: HistoryOutline) => checkOutline(o).map(p => p.kind);

  it('accepts a sound outline and reports each rule the schema cannot express', () => {
    expect(checkOutline(TREE)).toEqual([]);
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

describe('Python and TypeScript agree on positions', () => {
  it('every fixture position equals the rebuilder one', () => {
    for (const file of files) {
      const data = JSON.parse(readFileSync(join(DIR, file), 'utf8'));
      expect(checkOutline(data.outline)).toEqual([]);
      for (const ex of data.expectations) {
        if (ex.result.status === 'ok') {
          expect(positionAt(data.outline, ex.branchId, ex.tick)).toEqual(
            ex.result.state.position
          );
        }
      }
    }
  });
});
