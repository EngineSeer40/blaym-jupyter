import { AuthorMarker } from 'blaym-notebook-core';
import type { AuthorSignal } from 'blaym-notebook-core';
import { createSignalsFor } from '../../adapters/jupyterlab/signals';

function setup(provider?: string) {
  let handler: ((s: unknown, a: { id: string }) => void) | undefined;
  let now = 1000;
  const got: AuthorSignal[] = [];
  const signals = createSignalsFor({
    commands: {
      commandExecuted: {
        connect: f => void (handler = f),
        disconnect: () => void (handler = undefined)
      }
    },
    clock: { nowMs: () => now },
    activeCellId: () => 'c1',
    lastProvider: () => provider
  });
  const off = signals.onSignal(s => got.push(s));
  return {
    run: (id: string) => handler?.(null, { id }),
    got,
    off,
    at: (t: number) => (now = t),
    has: () => !!handler
  };
}

describe('JupyterLab author signals', () => {
  test('AI tool commands signal ai/known with the active cell; other commands do not', () => {
    const s = setup();
    s.run('jupyterlab-ai-commands:add-cell');
    s.run('notebook:run-cell');
    expect(s.got).toEqual([
      { kind: 'ai', confidence: 'known', atMs: 1000, cellId: 'c1' }
    ]);
    s.off();
    expect(s.has()).toBe(false);
  });

  test('window boundaries: 0 and 249 ms match on either side, 251 does not', () => {
    for (const [offset, want] of [
      [0, 'ai'],
      [249, 'ai'],
      [-249, 'ai'],
      [251, 'unknown'],
      [-251, 'unknown']
    ] as const) {
      let cb: ((s: AuthorSignal) => void) | undefined;
      const m = new AuthorMarker({
        signals: { onSignal: f => ((cb = f), () => undefined) },
        clock: { nowMs: () => 5000 },
        scheduler: { setTimeout: () => ({ cancel: () => undefined }) } as never,
        boundary: () => undefined
      });
      cb!({
        kind: 'ai',
        confidence: 'known',
        atMs: 5000 + offset,
        cellId: 'c1'
      });
      expect({ offset, hint: m.mark('c1', 5000, 5000).hint }).toEqual({
        offset,
        hint: want
      });
    }
  });
});
