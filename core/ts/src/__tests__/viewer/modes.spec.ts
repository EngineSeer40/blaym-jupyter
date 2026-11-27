import type { ContextEntry } from '../../viewer/context';
import { appendEntries, versions } from '../../viewer/modes';

let tick = 0;
const e = (kind: string, over: Partial<ContextEntry> = {}): ContextEntry => ({
  tick: ++tick,
  timeMs: 1763002106727 + tick * 1000,
  kind,
  author: 'person',
  confidence: 'known',
  cellId: null,
  runId: null,
  ...over
});

describe('viewer version modes', () => {
  test('grouped merges edits of one cell, shows a run, an edge and an unknown kind as one item each; every step lists all', () => {
    const index = [
      e('edit', { cellId: 'c2' }),
      e('edit', { cellId: 'c2' }),
      e('edit', { cellId: 'c2' }),
      e('run_started', { cellId: 'c3', runId: 'r1' }),
      e('text_output', { runId: 'r1' }),
      e('run_ok', { cellId: 'c3', runId: 'r1' }),
      e('edit', { cellId: 'c2' }),
      e('kernel_restart'),
      e('made_up_kind', { author: 'unknown' })
    ];
    const grouped = versions(index, { mode: 'grouped' });
    expect(grouped.map(i => i.summary)).toEqual([
      'edited cell c2 (3 edits)',
      'ran cell c3: ok',
      'edited cell c2',
      'kernel restarted',
      'unknown: made_up_kind'
    ]);
    expect(grouped[0]).toMatchObject({
      firstTick: index[0].tick,
      lastTick: index[2].tick
    });
    expect(grouped[1]).toMatchObject({
      firstTick: index[3].tick,
      lastTick: index[5].tick,
      kind: 'run_ok'
    });
    expect(versions(index, { mode: 'every' })).toHaveLength(index.length);
  });

  test('appendEntries appends in order, ignores held ticks and signals a gap', () => {
    const held = [e('edit', { tick: 1 }), e('edit', { tick: 2 })];
    const more = [e('edit', { tick: 2 }), e('run_ok', { tick: 3 })];
    const ok = appendEntries(held, more);
    expect(ok.entries.map(x => x.tick)).toEqual([1, 2, 3]);
    expect(ok.gap).toBe(false);
    expect(appendEntries(held, [e('edit', { tick: 5 })]).gap).toBe(true);
  });
});
