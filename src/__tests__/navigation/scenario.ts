import type {
  HistoryAdvanced,
  HistoryOutline,
  OutlineBranch,
  RebuildResult
} from 'blaym-jupyter-types';
import {
  initialState,
  navigator,
  INavState,
  IRefusal,
  NavInput,
  NavRequest
} from '../../navigation';

/**
 * A small readable harness for navigation scenarios, as the Python `Scenario`
 * class is for rebuilding. Every state and input is deep-frozen before the
 * navigator sees it, so a mutation fails the test.
 */

export interface IBranchSpec {
  id: string;
  /** Branch point: the parent branch and the parent's last tick included. */
  from?: [string, number];
  /** The branch's own ticks. */
  ticks: number[];
  /** Runs as [startedTick, endedTick?]. */
  runs?: Array<[number, number?]>;
}

export function range(first: number, last: number): number[] {
  const out: number[] = [];
  for (let t = first; t <= last; t++) {
    out.push(t);
  }
  return out;
}

export function branchEntry(spec: IBranchSpec): OutlineBranch {
  return {
    branchId: spec.id,
    name: spec.id,
    ...(spec.from
      ? { parentBranchId: spec.from[0], parentTick: spec.from[1] }
      : {}),
    ownTicks: spec.ticks,
    runSpans: (spec.runs ?? []).map(([s, e], i) => ({
      runId: `${spec.id}-run${i}`,
      startedTick: s,
      ...(e === undefined ? {} : { endedTick: e })
    }))
  };
}

/** The first spec is the root. */
export function outlineOf(...specs: IBranchSpec[]): HistoryOutline {
  const all = new Set<number>();
  specs.forEach(s => s.ticks.forEach(t => all.add(t)));
  let highest = 0;
  while (all.has(highest + 1)) {
    highest += 1;
  }
  return {
    schemaVersion: 2,
    explorationId: 'x',
    rootBranchId: specs[0].id,
    highestTick: highest,
    branches: specs.map(branchEntry)
  };
}

export function deepFreeze<T>(value: T): T {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    Object.values(value as object).forEach(v => deepFreeze(v));
  }
  return value;
}

/** A good answer for a position, with only the fields navigation reads. */
export function okAt(branchId: string, tick: number): RebuildResult {
  return {
    status: 'ok',
    state: {
      position: { branchId, tick, lastEventTick: tick, branchCount: 1 }
    }
  } as unknown as RebuildResult;
}

export class Scenario {
  state: INavState;
  /** What the last input asked the server to do. */
  requests: NavRequest[] = [];
  /** Why the last input did nothing, if it did nothing. */
  refusal?: IRefusal;
  /** Every request ever emitted, in order. */
  allRequests: NavRequest[] = [];

  /** Starts with the outline delivered and the first answer shown. */
  constructor(outline: HistoryOutline, active?: string) {
    this.state = deepFreeze(initialState(active ?? outline.rootBranchId));
    this.send({ type: 'outlineReceived', outline });
    this.answer();
  }

  send(input: NavInput): this {
    const before = this.state;
    const result = navigator(before, deepFreeze(input));
    this.state = deepFreeze(result.state);
    this.requests = result.requests;
    this.refusal = result.refusal;
    this.allRequests = this.allRequests.concat(result.requests);
    return this;
  }

  /** The newest fetchState the navigator has asked for. */
  lastFetch(): Extract<NavRequest, { type: 'fetchState' }> {
    const fetches = this.allRequests.filter(r => r.type === 'fetchState');
    return fetches[fetches.length - 1] as Extract<
      NavRequest,
      { type: 'fetchState' }
    >;
  }

  /** The server answers the newest fetchState with a good state. */
  answer(): this {
    const f = this.lastFetch();
    return this.send({
      type: 'stateReceived',
      requestNo: f.requestNo,
      result: okAt(f.branchId, f.tick)
    });
  }

  advance(
    branchId: string,
    tick: number,
    extra: Partial<HistoryAdvanced> = {}
  ): this {
    return this.send({
      type: 'historyAdvanced',
      notification: { explorationId: 'x', branchId, tick, ...extra }
    });
  }

  /** "branch@tick" for where the view is. */
  get at(): string {
    return `${this.state.view.branchId}@${this.state.view.tick}`;
  }

  /** "branch@tick" of the answer on screen, if any. */
  get shownAt(): string | undefined {
    const s = this.state.view.shown;
    return s && `${s.branchId}@${s.tick}`;
  }

  get active(): string {
    return this.state.active.branchId;
  }
}
