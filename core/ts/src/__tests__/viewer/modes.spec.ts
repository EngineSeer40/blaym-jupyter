import type { ContextEntry } from '../../viewer/context';
import { appendEntries, sessions, versions } from '../../viewer/modes';

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

  test('appendEntries appends in order, ignores held ticks, allows other branches ticks between and signals a contradiction', () => {
    const held = [e('edit', { tick: 1 }), e('edit', { tick: 2 })];
    const more = [e('edit', { tick: 2 }), e('run_ok', { tick: 3 })];
    const ok = appendEntries(held, more);
    expect(ok.entries.map(x => x.tick)).toEqual([1, 2, 3]);
    expect(ok.gap).toBe(false);
    expect(appendEntries(held, [e('edit', { tick: 5 })]).gap).toBe(false);
    expect(appendEntries(held, [e('edit', { tick: 1.5 })]).gap).toBe(true);
  });

  test('sessions split at a gap, a kernel start, a snapshot and an idle stretch; count; union the marks', () => {
    const at = (
      kind: string,
      minute: number,
      over: Partial<ContextEntry> = {}
    ) => e(kind, { timeMs: Date.UTC(2025, 10, 13, 2, minute), ...over });
    const index = [
      at('kernel_start', 0),
      at('insert', 1, { cellIndex: 0, cellCount: 2 }),
      at('edit', 2, { cellId: 'c1', cellIndex: 1, cellCount: 2, author: 'ai' }),
      at('run_started', 3, { runId: 'r1' }),
      at('chart', 4, { runId: 'r1' }),
      at('run_ok', 4, { runId: 'r1' }),
      at('gap', 5),
      at('edit', 6, { cellId: 'c1' }),
      at('kernel_restart', 7),
      at('snapshot', 8),
      at('edit', 9, { cellId: 'c1' }),
      at('edit', 50, { cellId: 'c1' })
    ];
    const items = sessions(index, { idleMs: 30 * 60 * 1000 });
    expect(items.map(i => [i.firstTick, i.lastTick])).toEqual([
      [index[0].tick, index[6].tick],
      [index[7].tick, index[7].tick],
      [index[8].tick, index[8].tick],
      [index[9].tick, index[10].tick],
      [index[11].tick, index[11].tick]
    ]);
    expect(items[0].summary).toBe(
      'session · 11-13 02:00 to 02:05 · 1 edit, 1 run, 1 chart, 1 AI change'
    );
    expect(items[0].marks.map(m => m.tone)).toEqual(['added', 'changed']);
    expect(versions(index, { mode: 'sessions' })).toHaveLength(5);
  });
});
