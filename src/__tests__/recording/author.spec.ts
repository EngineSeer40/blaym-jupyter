import { AuthorMarker, AuthorSignal } from '../../recording';
import { Harness } from './generate';

function make() {
  let cb: ((s: AuthorSignal) => void) | undefined;
  const h = new Harness(undefined, {
    marker: hh =>
      new AuthorMarker({
        signals: { onSignal: f => ((cb = f), () => undefined) },
        clock: hh.time,
        scheduler: hh.time,
        boundary: id => hh.grouper.boundary(id)
      })
  });
  const signal = (
    kind: 'ai' | 'person',
    atMs: number,
    cellId?: string,
    confidence: 'known' | 'guess' = 'known'
  ) => cb!({ kind, confidence, atMs, cellId });
  return { h, signal };
}

async function type(h: Harness, id: string, code: string) {
  h.nb.list.find(c => c.id === id)!.source = code;
  await h.change();
}

describe('AuthorMarker (S5-L, O2)', () => {
  test('no signal: unknown, with no confidence field', async () => {
    const { h } = make();
    const id = h.nb.insert(0, 'code', '');
    await h.snapshot();
    await type(h, id, 'a');
    await h.finish();
    const e = h.events[h.events.length - 1] as any;
    expect(e.authorHint).toBe('unknown');
    expect('authorConfidence' in e).toBe(false);
  });

  test('a signal that overlaps the change marks it ai/known, on either side within the window', async () => {
    for (const offset of [-250, -100, 0, 100, 250]) {
      const { h, signal } = make();
      const id = h.nb.insert(0, 'code', '');
      await h.snapshot();
      await type(h, id, 'accepted');
      signal('ai', h.time.nowMs() + offset, id);
      await h.finish();
      const e = h.events[h.events.length - 1] as any;
      expect({ offset, hint: e.authorHint, conf: e.authorConfidence }).toEqual({
        offset,
        hint: 'ai',
        conf: 'known'
      });
    }
  });

  test('a signal outside the window or for another cell does not mark', async () => {
    const { h, signal } = make();
    const id = h.nb.insert(0, 'code', '');
    const other = h.nb.insert(1, 'code', '');
    await h.snapshot();
    await type(h, id, 'a');
    signal('ai', h.time.nowMs() + 251, id);
    signal('ai', h.time.nowMs(), other);
    await h.finish();
    expect((h.events[h.events.length - 1] as any).authorHint).toBe('unknown');
  });

  test('person then AI in one cell stay two steps; typing after the AI is a third', async () => {
    const { h, signal } = make();
    const id = h.nb.insert(0, 'code', '');
    await h.snapshot();
    await type(h, id, 'my own typing');
    h.time.advance(300); // a pause, then the AI action: the signal ends the person's step
    signal('ai', h.time.nowMs(), id);
    await type(h, id, 'my own typing\nai wrote this');
    h.time.advance(400); // past the window: the boundary after the signal ends the AI step
    await type(h, id, 'my own typing\nai wrote this\nthen me');
    h.time.advance(3000);
    await h.finish();
    const steps = h.events.filter(
      e => e.type === 'cell_source_changed'
    ) as any[];
    expect(steps.map(s => [s.payload.code, s.authorHint])).toEqual([
      ['my own typing', 'unknown'],
      ['my own typing\nai wrote this', 'ai'],
      ['my own typing\nai wrote this\nthen me', 'unknown']
    ]);
    expect('authorConfidence' in steps[0]).toBe(false);
  });

  test('a person edit well before the AI action stays unknown and separate', async () => {
    const { h, signal } = make();
    const id = h.nb.insert(0, 'code', '');
    await h.snapshot();
    await type(h, id, 'person');
    h.time.advance(2000);
    signal('ai', h.time.nowMs(), id);
    await type(h, id, 'person\nai');
    h.time.advance(1000); // past the window: the boundary after the signal ends the AI step
    await type(h, id, 'person\nai\nperson again');
    h.time.advance(3000);
    await h.finish();
    const steps = h.events.filter(
      e => e.type === 'cell_source_changed'
    ) as any[];
    expect(steps.map(s => [s.payload.code, s.authorHint])).toEqual([
      ['person', 'unknown'],
      ['person\nai', 'ai'],
      ['person\nai\nperson again', 'unknown']
    ]);
  });

  test('a known signal wins over a guess in the same window; a person signal marks person', async () => {
    const { h, signal } = make();
    const id = h.nb.insert(0, 'code', '');
    await h.snapshot();
    const t = h.time.nowMs();
    signal('ai', t, id, 'guess');
    signal('ai', t, id, 'known');
    expect(h.marker!.mark(id, t, t)).toEqual({
      hint: 'ai',
      confidence: 'known'
    });
    signal('person', t + 5000, id, 'known');
    expect(h.marker!.mark(id, t + 5000, t + 5000)).toEqual({
      hint: 'person',
      confidence: 'known'
    });
    expect(h.marker!.mark(id, t + 2000, t + 2000)).toEqual({ hint: 'unknown' });
  });
});
