import * as fc from 'fast-check';
import type {
  HistoryOutline,
  OutlineBranch,
  OutlineRunSpan
} from 'blaym-notebook-types';

/**
 * Test helper: generates sound outlines with run spans, and a deliberately
 * simple independent model of "is a run open here". It does not use
 * src/navigation/line.ts.
 */

export type EventKind = 'plain' | 'start' | 'finish' | 'restart';

export interface ISimpleEvent {
  tick: number;
  branchId: string;
  kind: EventKind;
  runId?: string;
}

export interface ISimpleBranch {
  branchId: string;
  parentBranchId?: string;
  parentTick?: number;
}

export interface ISimpleHistory {
  branches: ISimpleBranch[];
  events: ISimpleEvent[];
}

export interface IOp {
  kind: 'event' | 'branch';
  sub: number;
  pick: number;
  at: number;
}

export const opsArb: fc.Arbitrary<IOp[]> = fc.array(
  fc.record({
    kind: fc.constantFrom<'event' | 'branch'>(
      'event',
      'event',
      'event',
      'branch'
    ),
    sub: fc.nat(1000),
    pick: fc.nat(1000),
    at: fc.nat(1000)
  }),
  { minLength: 1, maxLength: 60 }
);

/** The line of a branch as events, by plain recursion. */
export function lineEvents(
  h: ISimpleHistory,
  branchId: string
): ISimpleEvent[] {
  const b = h.branches.find(x => x.branchId === branchId) as ISimpleBranch;
  const own = h.events.filter(e => e.branchId === branchId);
  if (b.parentBranchId === undefined) {
    return own;
  }
  const parent = lineEvents(h, b.parentBranchId).filter(
    e => e.tick <= (b.parentTick as number)
  );
  return parent.concat(own);
}

/** Runs open after applying every line event up to and including `tick`. */
export function openRunsAt(
  h: ISimpleHistory,
  branchId: string,
  tick: number
): string[] {
  let open: string[] = [];
  for (const e of lineEvents(h, branchId)) {
    if (e.tick > tick) {
      break;
    }
    if (e.kind === 'start') {
      open.push(e.runId as string);
    } else if (e.kind === 'finish') {
      open = open.filter(r => r !== e.runId);
    } else if (e.kind === 'restart') {
      open = [];
    }
  }
  return open;
}

export function buildHistory(ops: IOp[]): ISimpleHistory {
  const h: ISimpleHistory = {
    branches: [{ branchId: 'b0' }],
    events: [{ tick: 1, branchId: 'b0', kind: 'plain' }]
  };
  let tick = 1;
  let runs = 0;
  for (const op of ops) {
    const target = h.branches[op.pick % h.branches.length].branchId;
    if (op.kind === 'branch') {
      const line = lineEvents(h, target);
      const parentTick = line[op.at % line.length].tick;
      tick += 1;
      const id = `b${h.branches.length}`;
      h.branches.push({ branchId: id, parentBranchId: target, parentTick });
      h.events.push({ tick, branchId: id, kind: 'plain' });
      continue;
    }
    tick += 1;
    const open = openRunsAt(h, target, Infinity);
    const s = op.sub % 6;
    if (s <= 1) {
      runs += 1;
      h.events.push({
        tick,
        branchId: target,
        kind: 'start',
        runId: `r${runs}`
      });
    } else if (s <= 3 && open.length) {
      h.events.push({
        tick,
        branchId: target,
        kind: 'finish',
        runId: open[op.at % open.length]
      });
    } else if (s === 4) {
      h.events.push({ tick, branchId: target, kind: 'restart' });
    } else {
      h.events.push({ tick, branchId: target, kind: 'plain' });
    }
  }
  return h;
}

/** Spans by the Python rules: own runs plus ancestor runs open at the branch point. */
export function outlineOfHistory(h: ISimpleHistory): HistoryOutline {
  const branches: OutlineBranch[] = h.branches.map(b => {
    const line = lineEvents(h, b.branchId);
    const own = h.events.filter(e => e.branchId === b.branchId);
    const ownTicks = own.map(e => e.tick);
    const inherited =
      b.parentBranchId === undefined
        ? []
        : openRunsAt(h, b.parentBranchId, b.parentTick as number);
    const spans: { [id: string]: OutlineRunSpan } = {};
    const order: string[] = [];
    for (const e of line) {
      if (e.kind === 'start') {
        spans[e.runId as string] = {
          runId: e.runId as string,
          startedTick: e.tick
        };
        order.push(e.runId as string);
      } else if (e.kind === 'finish') {
        const s = spans[e.runId as string];
        if (s && s.endedTick === undefined) {
          s.endedTick = e.tick;
        }
      } else if (e.kind === 'restart') {
        for (const id of order) {
          if (spans[id].endedTick === undefined) {
            spans[id].endedTick = e.tick;
          }
        }
      }
    }
    const runSpans = order
      .filter(
        id =>
          ownTicks.indexOf(spans[id].startedTick) >= 0 ||
          inherited.indexOf(id) >= 0
      )
      .map(id => spans[id]);
    return {
      branchId: b.branchId,
      name: b.branchId,
      ownTicks,
      runSpans,
      ...(b.parentBranchId === undefined
        ? {}
        : { parentBranchId: b.parentBranchId, parentTick: b.parentTick })
    };
  });
  return {
    schemaVersion: 2,
    explorationId: 'x',
    rootBranchId: 'b0',
    highestTick: Math.max(...h.events.map(e => e.tick)),
    branches
  };
}
