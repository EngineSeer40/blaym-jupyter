import type { HistoryOutline } from 'blaym-notebook-types';
import { createTimelines } from '../../viewer/timelines';

/** Main owns ticks 1 to 3, and "alt" branches at 2 and owns 5 and 6 (global ticks). */
const lineOf: Record<string, number[]> = { b1: [1, 2, 3], b2: [1, 2, 5, 6] };

function setup() {
  const outline = {
    rootBranchId: 'b1',
    highestTick: 6,
    branches: [
      { branchId: 'b1', name: 'main', ownTicks: [1, 2, 3], runSpans: [] },
      {
        branchId: 'b2',
        name: 'alt',
        parentBranchId: 'b1',
        parentTick: 2,
        ownTicks: [5, 6],
        runSpans: []
      }
    ]
  } as unknown as HistoryOutline;
  const fetched: string[] = [];
  const client = {
    outline: async () => outline,
    timeline: async (r: { branchId: string; fromTick?: number }) => {
      fetched.push(`${r.branchId}@${r.fromTick ?? 0}`);
      const ticks = lineOf[r.branchId].filter(t => t >= (r.fromTick ?? 0));
      const col = <T>(v: T) => ticks.map(() => v);
      return {
        ticks,
        timesMs: col(0),
        kinds: col('edit'),
        authors: col('person'),
        confidences: col(null),
        cellIds: col(null),
        runIds: col(null),
        truncated: false
      };
    }
  };
  let viewed = 'b1';
  let tip = 3;
  let active = 'b1';
  const session = {
    view: () => ({ branchId: viewed }),
    activeBranchId: () => active,
    activeTip: () => tip,
    branch: async () => 'b1'
  };
  const timelines = createTimelines({
    client: client as never,
    explorationId: 'e1',
    session: session as never
  });
  return {
    outline,
    timelines,
    fetched,
    view: (b: string) => (viewed = b),
    tipTo: (t: number) => (tip = t),
    activate: (b: string) => (active = b)
  };
}

const ticks = (es: { tick: number }[]) => es.map(e => e.tick);

describe('timelines', () => {
  it('keeps each branch its own ticks, gives the viewed line to the views and appends live ticks to the active branch only', async () => {
    const t = setup();
    await t.timelines.sync();
    expect(ticks(t.timelines.own('b1'))).toEqual([1, 2, 3]);
    expect(ticks(t.timelines.own('b2'))).toEqual([5, 6]);
    expect(ticks(t.timelines.viewed())).toEqual([1, 2, 3]);
    t.view('b2');
    expect(ticks(t.timelines.viewed())).toEqual([1, 2, 5, 6]);

    // The outline reports the new tick as the stream does.
    lineOf.b1 = [1, 2, 3, 4];
    (t.outline.branches[0].ownTicks as number[]).push(4);
    t.outline.highestTick = 7;
    t.tipTo(4);
    t.fetched.length = 0;
    await t.timelines.sync();
    expect(t.fetched).toEqual(['b1@4']);
    expect(ticks(t.timelines.own('b1'))).toEqual([1, 2, 3, 4]);
    expect(ticks(t.timelines.own('b2'))).toEqual([5, 6]);

    // Another branch's tick (5) lies between 4 and 6: no gap, only the new entries are fetched.
    lineOf.b1 = [1, 2, 3, 4, 6, 7];
    t.tipTo(7);
    t.fetched.length = 0;
    await t.timelines.sync();
    expect(t.fetched).toEqual(['b1@5']);
    t.view('b1');
    expect(ticks(t.timelines.viewed())).toEqual([1, 2, 3, 4, 6, 7]);

    // A branch's own ticks change in the outline: it is read again, though it is not active.
    lineOf.b2 = [1, 2, 5, 6, 8];
    (t.outline.branches[1].ownTicks as number[]).push(8);
    t.fetched.length = 0;
    await t.timelines.sync();
    expect(t.fetched).toEqual(['b2@0']);

    // A branch becoming active is read afresh, not appended to.
    lineOf.b2 = [1, 2, 5, 6, 8, 9];
    t.activate('b2');
    t.tipTo(9);
    t.fetched.length = 0;
    await t.timelines.sync();
    expect(t.fetched).toContain('b2@0');
    t.view('b2');
    expect(ticks(t.timelines.viewed())).toEqual([1, 2, 5, 6, 8, 9]);
  });
});
