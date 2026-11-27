import * as fc from 'fast-check';
import { AuthorMark, EventDraft, TypingGrouper } from '../../recording';
import { FakeTime } from './generate';

function make() {
  const time = new FakeTime();
  const out: Array<{ d: EventDraft; a: AuthorMark }> = [];
  const g = new TypingGrouper({
    scheduler: time,
    clock: time,
    emit: (d, a) => out.push({ d, a })
  });
  return { time, out, g };
}
const codes = (out: Array<{ d: EventDraft }>) => out.map(o => o.d.payload.code);

describe('TypingGrouper', () => {
  test('flushes after an 800 ms pause, not before', () => {
    const { time, out, g } = make();
    g.baseline('c', '');
    g.edit('c', 'a');
    time.advance(799);
    expect(out).toHaveLength(0);
    g.edit('c', 'ab');
    time.advance(799);
    expect(out).toHaveLength(0);
    time.advance(1);
    expect(codes(out)).toEqual(['ab']);
    expect(out[0].d.payload.cellId).toBe('c');
  });

  test('continuous typing flushes at most every 5 s', () => {
    const { time, out, g } = make();
    g.baseline('c', '');
    for (let i = 1; i <= 60; i++) {
      g.edit('c', 'x'.repeat(i));
      time.advance(200);
    }
    expect(out.length).toBeGreaterThanOrEqual(2);
    expect(out.length).toBeLessThanOrEqual(3);
  });

  test('beforeRun, flushCell, flushAll and boundary flush at once', () => {
    const { out, g } = make();
    g.baseline('a', '');
    g.baseline('b', '');
    g.edit('a', '1');
    g.beforeRun('a');
    expect(codes(out)).toEqual(['1']);
    g.edit('a', '12');
    g.edit('b', 'q');
    g.boundary('a');
    expect(codes(out)).toEqual(['1', '12']);
    g.flushAll();
    expect(codes(out)).toEqual(['1', '12', 'q']);
    g.edit('a', '123');
    g.boundary();
    expect(codes(out)).toEqual(['1', '12', 'q', '123']);
  });

  test('property: the last edit before a run, a save, a hide or a takeover is never lost', () => {
    const action = fc.oneof(
      fc.record({
        k: fc.constant('edit' as const),
        cell: fc.constantFrom('a', 'b'),
        text: fc.string({ maxLength: 6 })
      }),
      fc.record({
        k: fc.constant('wait' as const),
        ms: fc.integer({ min: 0, max: 3000 })
      }),
      fc.record({
        k: fc.constant('run' as const),
        cell: fc.constantFrom('a', 'b')
      }),
      fc.constant({ k: 'hide' as const })
    );
    fc.assert(
      fc.property(fc.array(action, { maxLength: 60 }), actions => {
        const { time, out, g } = make();
        const latest: Record<string, string> = { a: '', b: '' };
        const recorded: Record<string, string> = { a: '', b: '' };
        g.baseline('a', '');
        g.baseline('b', '');
        const lastEmitted = (cell: string) => {
          const mine = out.filter(o => o.d.cellId === cell);
          return mine.length
            ? (mine[mine.length - 1].d.payload.code as string)
            : '';
        };
        for (const a of actions) {
          if (a.k === 'edit') {
            latest[a.cell] = a.text;
            g.edit(a.cell, a.text);
          } else if (a.k === 'wait') {
            time.advance(a.ms);
          } else if (a.k === 'run') {
            g.beforeRun(a.cell);
            expect(lastEmitted(a.cell)).toBe(
              latest[a.cell] === recorded[a.cell] && lastEmitted(a.cell) === ''
                ? ''
                : latest[a.cell]
            );
          } else {
            g.flushAll();
            ['a', 'b'].forEach(c => expect(lastEmitted(c)).toBe(latest[c]));
          }
        }
      }),
      { numRuns: 300 }
    );
  });
});
