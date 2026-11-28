import type { SearchHit } from 'blaym-notebook-types';
import type { ContextEntry } from '../../viewer/context';
import { hitRows } from '../../viewer/search';

const entry = (tick: number, cellId: string | null): ContextEntry => ({
  tick,
  timeMs: tick * 1000,
  kind: 'edit',
  author: 'person',
  confidence: null,
  cellId,
  runId: null
});

describe('search hits to rows', () => {
  it('folds hits of one (branch, tick, kind), lists newest first and names the click target', () => {
    const hit = (branchId: string, tick: number, kind: SearchHit['kind']) => ({
      branchId,
      tick,
      kind,
      snippet: `${branchId}-${tick}-${kind}`
    });
    const rows = hitRows(
      [
        hit('b1', 3, 'code'),
        hit('b1', 3, 'code'),
        hit('b2', 9, 'chart_text'),
        hit('b1', 3, 'chat')
      ],
      (b, t) => (b === 'b2' && t === 9 ? entry(9, 'c4') : undefined)
    );
    expect(rows.map(r => `${r.branchId}:${r.tick}:${r.kind}`)).toEqual([
      'b2:9:chart_text',
      'b1:3:code',
      'b1:3:chat'
    ]);
    expect(rows[0]).toMatchObject({ cellId: 'c4', timeMs: 9000 });
    expect(rows[1]).toMatchObject({ cellId: null, timeMs: null });
  });
});
