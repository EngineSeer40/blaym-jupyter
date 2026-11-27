import type { HistoryOutline, RebuildResult } from 'blaym-notebook-types';
import type { Client, StreamHandlers } from '../../viewer/client';
import { createSession } from '../../viewer/session';

const flush = async () => {
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
};

function outlineTo(tick: number): HistoryOutline {
  return {
    schemaVersion: 2,
    explorationId: 'e1',
    rootBranchId: 'b1',
    highestTick: tick,
    branches: [
      {
        branchId: 'b1',
        name: 'main',
        ownTicks: Array.from({ length: tick }, (_, i) => i + 1),
        runSpans: []
      }
    ]
  } as HistoryOutline;
}

const okAt = (tick: number) =>
  ({
    status: 'ok',
    state: { position: { branchId: 'b1', tick } }
  }) as unknown as RebuildResult;

function fakeClient() {
  const stateCalls: { tick: number; resolve(r: RebuildResult): void }[] = [];
  let tip = 5;
  let handlers: StreamHandlers | undefined;
  const log: string[] = [];
  const client = {
    outline: async () => {
      log.push('outline');
      return outlineTo(tip);
    },
    eventsSince: async (req: { afterTick: number }) => {
      log.push(`since ${req.afterTick}`);
      return { events: [], outline: outlineTo(tip) };
    },
    state: (req: { tick: number }) =>
      new Promise<RebuildResult>(resolve =>
        stateCalls.push({ tick: req.tick, resolve })
      ),
    stream: (_e: string, _a: number | undefined, h: StreamHandlers) => {
      handlers = h;
      return () => undefined;
    }
  } as unknown as Client;
  return {
    client,
    stateCalls,
    log,
    setTip: (t: number) => (tip = t),
    handlers: () => handlers as StreamHandlers
  };
}

describe('viewer session', () => {
  test('only the newest answer is shown when answers arrive out of order', async () => {
    const f = fakeClient();
    const session = createSession({
      client: f.client,
      explorationId: 'e1',
      activeBranchId: 'b1'
    });
    session.start();
    await flush();
    f.stateCalls[0].resolve(okAt(5));
    await flush();

    session.focus.goto('b1', 2);
    session.focus.goto('b1', 3);
    const second = f.stateCalls.length - 1;
    const first = second - 1;
    expect(f.stateCalls[first].tick).toBe(2);
    expect(f.stateCalls[second].tick).toBe(3);
    f.stateCalls[second].resolve(okAt(3));
    await flush();
    f.stateCalls[first].resolve(okAt(2));
    await flush();

    const view = session.view();
    expect(view.shown?.tick).toBe(3);
    expect(view.freshness).toBe('current');
    expect(session.focus.get().position).toEqual({ branchId: 'b1', tick: 3 });
  });

  test('select and hover leave the position alone, and a frame that skips ticks refetches the outline', async () => {
    const f = fakeClient();
    const session = createSession({
      client: f.client,
      explorationId: 'e1',
      activeBranchId: 'b1'
    });
    session.start();
    await flush();
    const seen: unknown[] = [];
    session.focus.subscribe(s => seen.push(s));
    const position = session.focus.get().position;
    const item = { firstTick: 2, lastTick: 3, kind: 'edit' };
    session.focus.select(item);
    session.focus.hover(item);
    expect(session.focus.get().selected).toBe(item);
    expect(session.focus.get().hovered).toBe(item);
    expect(session.focus.get().position).toBe(position);
    expect(seen).toHaveLength(2);

    f.log.length = 0;
    f.setTip(8);
    f.handlers().onAdvanced({
      explorationId: 'e1',
      branchId: 'b1',
      tick: 8
    });
    await flush();
    expect(f.log).toContain('outline');
    expect(f.log).toContain('since 5');
    expect(session.view().tick).toBe(8);
  });
});
