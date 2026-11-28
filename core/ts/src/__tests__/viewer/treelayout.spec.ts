import type { HistoryOutline } from 'blaym-notebook-types';
import type { ContextEntry } from '../../viewer/context';
import {
  binsAt,
  connectorEnd,
  layoutTree,
  rowsAt
} from '../../viewer/treelayout';

const entry = (
  tick: number,
  kind = 'edit',
  author: ContextEntry['author'] = 'person'
): ContextEntry => ({
  tick,
  timeMs: tick * 1000,
  kind,
  author,
  confidence: author === 'ai' ? 'known' : null,
  cellId: 'c1',
  runId: null
});

const range = (a: number, b: number) =>
  Array.from({ length: b - a + 1 }, (_, i) => entry(a + i));

/**
 * main 1..30 and 58..59; a branches from main at 5 (own 31..35, 46..50); b branches from a
 * at 33 (own 36..45); c branches from main at 20 (own 53..57), after a and b have ended.
 */
const data: Record<string, ContextEntry[]> = {
  main: [...range(1, 30), ...range(58, 59)],
  a: [...range(31, 35), ...range(46, 50)],
  b: range(36, 45),
  c: [...range(53, 55), entry(56, 'run_error'), entry(57, 'chart')]
};
const branch = (id: string, parent?: string, parentTick?: number) => ({
  branchId: id,
  name: id,
  parentBranchId: parent,
  parentTick,
  ownTicks: data[id].map(e => e.tick),
  runSpans: []
});
const outline = {
  branches: [
    branch('main'),
    branch('a', 'main', 5),
    branch('b', 'a', 33),
    branch('c', 'main', 20)
  ]
} as unknown as HistoryOutline;

describe('tree layout', () => {
  it('puts nested and later branches in lanes, reusing one once its branch ended', () => {
    const t = layoutTree(outline, id => data[id]);
    const lane = (id: string) => t.lanes.find(l => l.branchId === id)!.lane;
    expect(lane('main')).toBe(0);
    expect(lane('a')).toBe(1);
    expect(lane('b')).toBe(2);
    // a and b have ended when c starts: c takes the lane next to its parent.
    expect(lane('c')).toBe(1);
    expect(t.laneCount).toBe(3);
    // b's connector starts on a's lane (a owns tick 33), not on main's.
    const cb = t.connectors.find(c => c.branchId === 'b')!;
    expect([cb.ownerId, cb.fromLane, cb.toLane]).toEqual(['a', 1, 2]);
    // a branches from the shared part of main: its connector leaves main at pos of tick 5.
    expect(t.connectors.find(c => c.branchId === 'a')).toMatchObject({
      ownerId: 'main',
      fromPos: 4,
      toPos: 30
    });
    expect(connectorEnd(100, 130)).toBe(130);
    expect(connectorEnd(100, 300)).toBe(144);
    expect(connectorEnd(300, 100)).toBe(256);
  });

  it('bins by zoom and keeps rows apart with a +N count', () => {
    const t = layoutTree(outline, id => data[id]);
    expect(binsAt(t, 0.02).filter(b => b.branchId === 'main')).toHaveLength(32);
    const far = binsAt(t, 3).find(b => b.branchId === 'c')!;
    expect(far.count).toBe(5);
    expect(far.top).toBe('run_error');
    expect(binsAt(t, 3).find(b => b.branchId === 'main')!.top).toBeNull();
    expect(binsAt(t, 3).some(b => b.share === 1)).toBe(true);
    // At 1 pos per px the 42 px gap folds the branches and the chart into the failed run.
    const rows = rowsAt(t, 1, (e, key, name) => `${key} ${name}`);
    expect(rows.map(r => r.key)).toEqual(['run_error']);
    expect(rows[0].more).toBe(4);
    const close = rowsAt(t, 0.01, (e, key) => key);
    expect(close.map(r => r.key)).toEqual([
      'branch',
      'branch',
      'branch',
      'run_error',
      'chart'
    ]);
    expect(close.every(r => r.more === 0)).toBe(true);
    // Two charts one position apart across a bin edge (pos 23 and 24 at 24 pos a bin) fold into one badge.
    const edge = {
      main: [...range(1, 23), entry(24, 'chart'), entry(25, 'chart')]
    };
    const split = layoutTree({ branches: [branch('main')] }, () => edge.main);
    const badges = binsAt(split, 1).filter(b => b.top === 'chart');
    expect(badges).toHaveLength(1);
    expect(badges[0].kinds.chart).toBe(2);
  });
});
