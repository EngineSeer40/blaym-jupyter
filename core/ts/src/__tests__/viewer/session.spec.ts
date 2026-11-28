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

  test('continue from here creates a branch, asks the host to switch, and a host failure leaves the view', async () => {
    const f = fakeClient();
    const created: unknown[] = [];
    const hostCalls: string[] = [];
    let answer: { status: 'ok' | 'failed'; reason?: string } = { status: 'ok' };
    (f.client as unknown as Record<string, unknown>).createBranch = async (
      req: unknown
    ) => {
      created.push(req);
      return {
        status: 'ok',
        branch: {
          branchId: 'b2',
          name: 'x',
          parentBranchId: 'b1',
          parentTick: 3,
          ownTicks: [],
          runSpans: []
        }
      };
    };
    const session = createSession({
      client: f.client,
      explorationId: 'e1',
      activeBranchId: 'b1',
      switchHost: async id => {
        hostCalls.push(id);
        return answer;
      }
    });
    session.start();
    await flush();
    f.stateCalls[0].resolve(okAt(5));
    await flush();
    session.focus.goto('b1', 3);
    await flush();
    f.stateCalls[1].resolve(okAt(3));
    await flush();

    session.continueFromHere();
    await flush();
    expect(created).toHaveLength(1);
    expect(hostCalls).toEqual(['b2']);
    expect(session.problem()).toBeUndefined();
    expect(session.activeBranchId()).toBe('b2');

    // A cancelled switch after create leaves the user where they were.
    const second = createSession({
      client: f.client,
      explorationId: 'e1',
      activeBranchId: 'b1',
      switchHost: async () => ({ status: 'cancelled', reason: 'cancelled' })
    });
    second.start();
    await flush();
    f.stateCalls[2].resolve(okAt(5));
    await flush();
    second.focus.goto('b1', 3);
    await flush();
    f.stateCalls[3].resolve(okAt(3));
    await flush();
    second.continueFromHere();
    await flush();
    expect(second.activeBranchId()).toBe('b1');
    expect(second.problem()).toBe('cancelled');
    second.stop();

    answer = { status: 'failed', reason: 'still on main' };
    session.switchTo('b1');
    await flush();
    expect(hostCalls).toEqual(['b2', 'b1']);
    expect(session.problem()).toBe('still on main');
    expect(session.activeBranchId()).toBe('b2');
  });
  test('returning to the original branch after jumping around shows its tip first, never a stale state', async () => {
    const b2 = {
      branchId: 'b2',
      name: 'branch',
      parentBranchId: 'b1',
      parentTick: 3,
      ownTicks: [6, 7],
      runSpans: []
    };
    let made = false;
    const stateAsked: { branchId: string; tick: number }[] = [];
    const client = {
      outline: async () => {
        const o = outlineTo(5);
        return made
          ? ({
              ...o,
              highestTick: 7,
              branches: [...o.branches, b2]
            } as HistoryOutline)
          : o;
      },
      eventsSince: async () => ({ events: [], outline: outlineTo(5) }),
      createBranch: async () => {
        made = true;
        return { status: 'ok', branch: b2 };
      },
      state: async (req: { branchId: string; tick: number }) => {
        stateAsked.push({ branchId: req.branchId, tick: req.tick });
        return {
          status: 'ok',
          state: { position: { branchId: req.branchId, tick: req.tick } }
        } as unknown as RebuildResult;
      },
      stream: () => () => undefined
    } as unknown as Client;
    const session = createSession({
      client,
      explorationId: 'e1',
      activeBranchId: 'b1',
      switchHost: async () => ({ status: 'ok' })
    });
    session.start();
    await flush();
    session.focus.goto('b1', 3);
    await flush();
    session.continueFromHere();
    await flush();
    expect(session.activeBranchId()).toBe('b2');
    expect(session.previousBranchId()).toBe('b1');
    session.focus.goto('b2', 6);
    await flush();
    session.focus.goto('b2', 4);
    await flush();

    const shown: { branchId: string; tick?: number; current: boolean }[] = [];
    session.subscribe(v =>
      shown.push({
        branchId: v.branchId,
        tick: v.shown?.tick,
        current: v.freshness === 'current'
      })
    );
    session.switchTo('b1');
    await flush();
    expect(session.activeBranchId()).toBe('b1');
    // A state still loading may show the last answer, marked as loading; what is current must be the tip.
    const onB1 = shown.filter(v => v.branchId === 'b1' && v.current);
    expect(onB1.length).toBeGreaterThan(0);
    expect(onB1.every(v => v.tick === 5)).toBe(true);
    expect(session.view().tick).toBe(5);
  });
});
