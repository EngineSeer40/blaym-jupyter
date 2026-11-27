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

describe('AuthorMarker', () => {
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
    signal('person', t + 9000, id);
    signal('ai', t + 9000, id);
    expect(h.marker!.mark(id, t + 9000, t + 9000).hint).toBe('ai');
  });
});
