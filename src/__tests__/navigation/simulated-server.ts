import type {
  HistoryAdvanced,
  HistoryOutline,
  OutlineBranch,
  RebuildResult
} from 'blaym-jupyter-types';
import type { NavInput, NavRequest } from '../../navigation';
import {
  ISimpleEvent,
  ISimpleHistory,
  lineEvents,
  openRunsAt,
  outlineOfHistory
} from './outline-gen';

/**
 * The simulated server for generated navigation sequences. It holds the
 * authoritative history as a plain list of events and answers the navigator's
 * requests. Every position and every "is a run open here" answer is worked out
 * with the deliberately simple model in outline-gen.ts (whole-line filtering),
 * never with src/navigation/line.ts.
 *
 * Messages to the client wait in a queue until the test delivers them, so the
 * test can reorder, duplicate or drop them.
 */

export interface IToClient {
  input: NavInput;
  /** Reliable messages (switch and branch answers) are never lost or duplicated. */
  reliable: boolean;
}

export interface IToServer {
  request: NavRequest;
  reliable: boolean;
}

export function maxTickOf(h: ISimpleHistory): number {
  return Math.max(...h.events.map(e => e.tick));
}

export function lineTicks(h: ISimpleHistory, branchId: string): number[] {
  return lineEvents(h, branchId).map(e => e.tick);
}

/** Position by the simple model: count and last event among line events up to `tick`. */
export function modelPosition(
  h: ISimpleHistory,
  branchId: string,
  tick: number
):
  | {
      branchId: string;
      tick: number;
      lastEventTick: number;
      branchCount: number;
    }
  | undefined {
  const line = lineEvents(h, branchId).filter(e => e.tick <= tick);
  if (!line.length) {
    return undefined;
  }
  return {
    branchId,
    tick,
    lastEventTick: line[line.length - 1].tick,
    branchCount: line.length
  };
}

/** The history as it was once tick `tick` had been stored. */
export function truncatedAt(h: ISimpleHistory, tick: number): ISimpleHistory {
  const events = h.events.filter(e => e.tick <= tick);
  const ids = new Set(events.map(e => e.branchId));
  return { branches: h.branches.filter(b => ids.has(b.branchId)), events };
}

export class SimulatedServer {
  readonly h: ISimpleHistory;
  /** The branch recording goes to. */
  active: string;
  connected = true;
  toClient: IToClient[] = [];
  toServer: IToServer[] = [];
  private runs: number;

  constructor(history: ISimpleHistory) {
    this.h = {
      branches: history.branches.map(b => ({ ...b })),
      events: history.events.map(e => ({ ...e }))
    };
    this.active = 'b0';
    this.runs = this.h.events.filter(e => e.kind === 'start').length;
  }

  outline(): HistoryOutline {
    return outlineOfHistory(this.h);
  }

  /** Adds an ordinary event, run start, run finish or kernel restart. */
  addEvent(branchId: string, sub: number, at: number): ISimpleEvent {
    const tick = maxTickOf(this.h) + 1;
    const open = openRunsAt(this.h, branchId, Infinity);
    const s = sub % 6;
    let e: ISimpleEvent;
    if (s <= 1) {
      this.runs += 1;
      e = { tick, branchId, kind: 'start', runId: `r${this.runs}` };
    } else if (s <= 3 && open.length) {
      e = { tick, branchId, kind: 'finish', runId: open[at % open.length] };
    } else if (s === 4) {
      e = { tick, branchId, kind: 'restart' };
    } else {
      e = { tick, branchId, kind: 'plain' };
    }
    this.h.events.push(e);
    return e;
  }

  /** Creates a branch off `parentId` at the line tick picked by `at`. */
  addBranch(parentId: string, at: number): ISimpleEvent {
    const line = lineEvents(this.h, parentId);
    return this.addBranchAt(parentId, line[at % line.length].tick);
  }

  private addBranchAt(parentId: string, parentTick: number): ISimpleEvent {
    const tick = maxTickOf(this.h) + 1;
    const id = `b${this.h.branches.length}`;
    this.h.branches.push({
      branchId: id,
      parentBranchId: parentId,
      parentTick
    });
    const e: ISimpleEvent = { tick, branchId: id, kind: 'plain' };
    this.h.events.push(e);
    return e;
  }

  /** The notification for one stored event, as the server would send it. */
  notificationFor(e: ISimpleEvent): HistoryAdvanced {
    const n: HistoryAdvanced = {
      explorationId: 'x',
      branchId: e.branchId,
      tick: e.tick
    };
    const first = this.h.events.find(x => x.branchId === e.branchId);
    const branch = this.h.branches.find(b => b.branchId === e.branchId);
    if (first === e && branch && branch.parentBranchId !== undefined) {
      n.newBranch = outlineOfHistory(truncatedAt(this.h, e.tick)).branches.find(
        b => b.branchId === e.branchId
      ) as OutlineBranch;
    }
    if (e.kind === 'start') {
      n.runStarted = e.runId;
    } else if (e.kind === 'finish') {
      n.runsEnded = [e.runId as string];
    } else if (e.kind === 'restart') {
      const open = openRunsAt(this.h, e.branchId, e.tick - 1);
      if (open.length) {
        n.runsEnded = open;
      }
    }
    return n;
  }

  /** Queues the notification for a new event, if the client is connected. */
  announce(e: ISimpleEvent): void {
    if (this.connected) {
      this.toClient.push({
        input: {
          type: 'historyAdvanced',
          notification: this.notificationFor(e)
        },
        reliable: false
      });
    }
  }

  /**
   * Answers one request. `b` and `c` are random numbers that decide whether the
   * answer is a typed problem or a failure.
   */
  serve(request: NavRequest, b: number, c: number): void {
    const send = (input: NavInput, reliable: boolean): void => {
      this.toClient.push({ input, reliable });
    };
    switch (request.type) {
      case 'fetchState': {
        const pos = modelPosition(this.h, request.branchId, request.tick);
        let result: RebuildResult;
        if (!pos) {
          result = {
            status: 'invalid_event',
            eventId: 'none',
            tick: request.tick,
            reason: 'unknown',
            detail: 'no event on this line'
          } as unknown as RebuildResult;
        } else if (b % 12 === 0) {
          result = {
            status: 'missing_events',
            firstMissingTick: request.tick
          } as unknown as RebuildResult;
        } else {
          result = {
            status: 'ok',
            state: {
              position: pos,
              token: `${pos.branchId}@${pos.lastEventTick}#${pos.branchCount}`
            }
          } as unknown as RebuildResult;
        }
        send(
          { type: 'stateReceived', requestNo: request.requestNo, result },
          false
        );
        return;
      }
      case 'fetchOutline':
        send({ type: 'outlineReceived', outline: this.outline() }, false);
        return;
      case 'fetchMissedSince': {
        for (const e of this.h.events.filter(x => x.tick > request.tick)) {
          send(
            { type: 'historyAdvanced', notification: this.notificationFor(e) },
            false
          );
        }
        return;
      }
      case 'createBranch': {
        const line = lineTicks(this.h, request.fromBranchId);
        if (!line.includes(request.fromTick) || c % 6 === 0) {
          send(
            { type: 'switchFailed', reason: 'could not create the branch' },
            true
          );
          return;
        }
        const e = this.addBranchAt(request.fromBranchId, request.fromTick);
        this.active = e.branchId;
        const branch = outlineOfHistory(
          truncatedAt(this.h, e.tick)
        ).branches.find(x => x.branchId === e.branchId) as OutlineBranch;
        send({ type: 'branchCreated', branch }, true);
        this.announce(e);
        return;
      }
      case 'switchBranch': {
        if (c % 6 === 0) {
          send({ type: 'switchFailed', reason: 'could not switch' }, true);
          return;
        }
        this.active = request.branchId;
        send({ type: 'switched', branchId: request.branchId }, true);
        return;
      }
    }
  }
}
