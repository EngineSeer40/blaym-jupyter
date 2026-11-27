import * as fc from 'fast-check';
import { alignCells } from '../../recording';

const tip = (n: number) =>
  Array.from({ length: n }, (_, i) => ({
    cellId: `B${i}`,
    type: 'code' as const,
    code: `src ${i}`
  }));

describe('alignCells (page open, unstable ids)', () => {
  test('identical notebook: every cell maps to its Blaym id', () => {
    const t = tip(4);
    const page = t.map((c, i) => ({
      id: `r${i}`,
      type: c.type,
      source: c.code
    }));
    const a = alignCells(page, t);
    expect(a.matches).toEqual({ r0: 'B0', r1: 'B1', r2: 'B2', r3: 'B3' });
    expect(a.newModelIds).toEqual([]);
    expect(a.deletedBlaymIds).toEqual([]);
  });

  test('property: a reopened 4.4 notebook with fresh random ids maps every unchanged cell, never two to one', () => {
    const arb = fc.record({
      n: fc.integer({ min: 0, max: 14 }),
      fates: fc.array(fc.constantFrom('keep', 'keep', 'keep', 'edit', 'drop'), {
        minLength: 14,
        maxLength: 14
      }),
      inserts: fc.array(fc.record({ at: fc.nat(20), md: fc.boolean() }), {
        maxLength: 4
      }),
      types: fc.array(fc.constantFrom('code', 'markdown'), {
        minLength: 14,
        maxLength: 14
      })
    });
    fc.assert(
      fc.property(arb, ({ n, fates, inserts, types }) => {
        const recorded = Array.from({ length: n }, (_, i) => ({
          cellId: `B${i}`,
          type: types[i] as 'code' | 'markdown',
          code: `unique recorded source ${i}`
        }));
        let page: Array<{
          id: string;
          type: any;
          source: string;
          unchanged?: string;
        }> = [];
        recorded.forEach((c, i) => {
          if (fates[i] === 'drop') return;
          page.push({
            id: `rand${Math.random()}`,
            type: c.type,
            source: fates[i] === 'edit' ? `edited ${i}` : c.code,
            unchanged: fates[i] === 'keep' ? c.cellId : undefined
          });
        });
        inserts.forEach((ins, k) => {
          page.splice(ins.at % (page.length + 1), 0, {
            id: `rand-new-${k}-${Math.random()}`,
            type: ins.md ? 'markdown' : 'code',
            source: `brand new ${k}`
          });
        });
        const a = alignCells(
          page.map(p => ({ id: p.id, type: p.type, source: p.source })),
          recorded
        );
        page.forEach(p => {
          if (p.unchanged) expect(a.matches[p.id]).toBe(p.unchanged);
        });
        const targets = Object.values(a.matches);
        expect(new Set(targets).size).toBe(targets.length);
        expect(Object.keys(a.matches).length + a.newModelIds.length).toBe(
          page.length
        );
        expect(targets.length + a.deletedBlaymIds.length).toBe(recorded.length);
      }),
      { numRuns: 300 }
    );
    expect(tip(0)).toEqual([]);
  });
});
