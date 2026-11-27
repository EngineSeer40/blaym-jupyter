import type { NotebookCell, RebuiltState } from 'blaym-notebook-types';
import type { ContextEntry } from '../../viewer/context';
import { diff } from '../../viewer/diff';
import { baseTick, createBaseCache } from '../../viewer/diffbase';

const cell = (cellId: string, code: string, hash?: string): NotebookCell =>
  ({
    cellId,
    cellType: 'code',
    code,
    revision: 1,
    metadata: {},
    outputs: hash
      ? [
          {
            runId: 'r',
            items: [{ outputType: 'stream', contentHash: hash }]
          }
        ]
      : []
  }) as unknown as NotebookCell;

const state = (cells: NotebookCell[]): RebuiltState =>
  ({ notebook: { path: 'n.ipynb', metadata: {}, cells } }) as RebuiltState;

describe('viewer diff', () => {
  test('marks added, deleted with its old index, moved, changed lines and changed outputs', () => {
    const base = state([
      cell('a', 'x = 1'),
      cell('gone', 'old'),
      cell('b', 'l1\nl2\nl3', 'h1'),
      cell('c', 'same'),
      cell('d', 'same')
    ]);
    const shown = state([
      cell('a', 'x = 1'),
      cell('b', 'l1\nL2\nl3', 'h2'),
      cell('d', 'same'),
      cell('c', 'same'),
      cell('new', 'fresh')
    ]);
    const r = diff(base, shown);
    expect(r.cells.has('a')).toBe(false);
    expect(r.cells.get('new')?.added).toBe(true);
    expect(r.deleted.map(d => [d.cell.cellId, d.index])).toEqual([['gone', 1]]);
    expect(r.cells.get('b')).toMatchObject({
      codeChanged: true,
      changedLines: [1],
      outputsChanged: true
    });
    const moved = ['c', 'd'].filter(id => r.cells.get(id)?.moved);
    expect(moved).toHaveLength(1);
  });

  test('the base for "now" is the tip and for "previous" the previous item\'s last tick', async () => {
    const entry = (tick: number, kind: string): ContextEntry => ({
      tick,
      timeMs: tick,
      kind,
      author: 'person',
      confidence: 'known',
      cellId: null,
      runId: null
    });
    const entries = [
      entry(1, 'kernel_restart'),
      entry(2, 'insert'),
      entry(3, 'delete'),
      entry(4, 'kernel_restart')
    ];
    const view = {
      mode: 'lookingBack',
      tick: 3,
      lookingBackFromTip: 9
    } as const;
    expect(baseTick('now', view, entries, 'grouped')).toBe(9);
    expect(baseTick('previous', view, entries, 'grouped')).toBe(2);
    expect(baseTick('previous', { ...view, tick: 1 }, entries, 'grouped')).toBe(
      undefined
    );
    expect(
      baseTick('now', { ...view, mode: 'following' }, entries, 'grouped')
    ).toBe(undefined);

    const asked: number[] = [];
    const cache = createBaseCache(
      {
        state: async req => {
          asked.push(req.tick);
          return { status: 'ok', state: state([]) } as never;
        }
      },
      'e'
    );
    await cache.load('b', 9);
    await cache.load('b', 9);
    expect(asked).toEqual([9]);
    expect(cache.peek('b', 9)).toBeDefined();
  });
});
