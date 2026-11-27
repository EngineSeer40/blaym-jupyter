import type {
  HistoryOutline,
  OutlineBranch,
  OutlineRunSpan
} from 'blaym-notebook-types';
import {
  checkOutline,
  isSettledAt,
  lineTicks,
  nextTick,
  positionAt,
  previousTick,
  tipOf
} from './line';
import type {
  Freshness,
  INavResult,
  INavState,
  IRefusal,
  IView,
  NavInput,
  NavRequest,
  RefusalKind
} from './types';

/**
 * The navigator: a pure function `(state, input) -> { state, requests, refusal? }`.
 *
 * It has no clock, I/O, randomness or UI, and never changes the state it is
 * given: every part it changes is rebuilt. Server answers come in as inputs;
 * what to ask the server comes out as request data.
 */

/** The state before the server has sent the outline. Recording goes to `activeBranchId`. */
export function initialState(activeBranchId: string): INavState {
  return {
    active: { branchId: activeBranchId },
    view: { mode: 'following', branchId: activeBranchId, freshness: 'loading' },
    newSinceLookingBack: 0,
    connection: { status: 'connected', lastTickSeen: 0 },
    lastRequestNo: 0
  };
}

/** Drops keys whose value is undefined, so equal states compare equal. */
function clean<T extends object>(o: T): T {
  const copy: any = { ...o };
  for (const key of Object.keys(copy)) {
    if (copy[key] === undefined) {
      delete copy[key];
    }
  }
  return copy as T;
}

function refuse(
  state: INavState,
  kind: RefusalKind,
  reason: string,
  extra: Partial<IRefusal> = {}
): INavResult {
  return { state, requests: [], refusal: { kind, reason, ...extra } };
}

function unchanged(state: INavState): INavResult {
  return { state, requests: [] };
}

function highestContiguous(outline: HistoryOutline, from: number): number {
  const ticks = new Set<number>();
  let max = from;
  for (const b of outline.branches) {
    for (const t of b.ownTicks) {
      ticks.add(t);
      if (t > max) {
        max = t;
      }
    }
  }
  let h = from;
  while (ticks.has(h + 1)) {
    h += 1;
  }
  return h;
}

function maxTickOf(outline: HistoryOutline): number {
  let max = 0;
  for (const b of outline.branches) {
    for (const t of b.ownTicks) {
      if (t > max) {
        max = t;
      }
    }
  }
  return max;
}

/** The count of new ticks on the active line since looking back began. */
function recount(state: INavState): INavState {
  let n = 0;
  if (state.view.mode === 'lookingBack' && state.outline) {
    const from = state.view.lookingBackFromTip ?? 0;
    n = lineTicks(state.outline, state.active.branchId).filter(
      t => t > from
    ).length;
  }
  return n === state.newSinceLookingBack
    ? state
    : { ...state, newSinceLookingBack: n };
}

/** Allocates the next request number and points the view at the answer. */
function ask(
  state: INavState,
  branchId: string,
  tick: number,
  freshness: Freshness
): INavResult {
  const requestNo = state.lastRequestNo + 1;
  const view: IView = clean({
    ...state.view,
    freshness,
    awaiting: requestNo
  });
  return {
    state: { ...state, lastRequestNo: requestNo, view },
    requests: [{ type: 'fetchState', requestNo, branchId, tick }]
  };
}

/**
 * Moves the view to a position and asks for its state. The view follows now
 * exactly when the position is the active branch's tip. A good answer already
 * shown for the position is reused.
 */
function showAt(
  prev: INavState,
  branchId: string,
  tick: number | undefined,
  freshness: Freshness
): INavResult {
  const following =
    tick !== undefined &&
    branchId === prev.active.branchId &&
    tick === prev.active.tip;
  const lookingBackFromTip = following
    ? undefined
    : prev.view.mode === 'following'
      ? prev.active.tip
      : prev.view.lookingBackFromTip;
  const base: INavState = {
    ...prev,
    view: clean({
      mode: following ? 'following' : 'lookingBack',
      branchId,
      tick,
      shown: prev.view.shown,
      freshness: 'loading' as Freshness,
      lookingBackFromTip
    }) as IView
  };
  const shown = prev.view.shown;
  if (
    tick !== undefined &&
    shown &&
    shown.result.status === 'ok' &&
    shown.branchId === branchId &&
    shown.tick === tick
  ) {
    return unchanged(
      recount({ ...base, view: { ...base.view, freshness: 'current' } })
    );
  }
  if (tick === undefined) {
    return unchanged(recount(base));
  }
  const asked = ask(base, branchId, tick, freshness);
  return { state: recount(asked.state), requests: asked.requests };
}

/** Installs a new outline, keeps the view where it should be, and recounts. */
function withOutline(
  prev: INavState,
  outline: HistoryOutline,
  extra: NavRequest[]
): INavResult {
  const tip = tipOf(outline, prev.active.branchId);
  const state: INavState = {
    ...prev,
    outline,
    active: clean({ ...prev.active, tip }),
    connection: {
      ...prev.connection,
      lastTickSeen: Math.max(prev.connection.lastTickSeen, outline.highestTick)
    }
  };
  if (state.view.mode === 'following' && state.view.tick !== tip) {
    const catching: Freshness =
      state.view.tick === undefined ? 'loading' : 'catchingUp';
    const moved = showAt(state, state.active.branchId, tip, catching);
    return { state: moved.state, requests: extra.concat(moved.requests) };
  }
  return { state: recount(state), requests: extra };
}

/** Makes `branchId` the active branch (a server answer) and follows its tip. */
function adoptActive(
  prev: INavState,
  outline: HistoryOutline,
  branchId: string,
  extra: NavRequest[]
): INavResult {
  const tip = tipOf(outline, branchId);
  const state: INavState = clean({
    ...prev,
    outline,
    active: clean({ branchId, tip }),
    pending: undefined,
    newSinceLookingBack: 0,
    connection: {
      ...prev.connection,
      lastTickSeen: Math.max(prev.connection.lastTickSeen, outline.highestTick)
    }
  });
  const moved = showAt(state, branchId, tip, 'loading');
  return { state: moved.state, requests: extra.concat(moved.requests) };
}

function sortedUnion(a: number[], b: number[]): number[] {
  const all = new Set<number>(a.concat(b));
  return Array.from(all).sort((x, y) => x - y);
}

/** Applies a run start and run ends at `tick` to one branch's spans. */
function withSpans(
  spans: OutlineRunSpan[],
  tick: number,
  runStarted?: string,
  runsEnded?: string[]
): OutlineRunSpan[] {
  const ended = runsEnded ?? [];
  const next = spans.map(sp =>
    sp.endedTick === undefined && ended.includes(sp.runId)
      ? { ...sp, endedTick: tick }
      : sp
  );
  return runStarted !== undefined && !next.some(sp => sp.runId === runStarted)
    ? next.concat([{ runId: runStarted, startedTick: tick }])
    : next;
}

function withBranch(
  outline: HistoryOutline,
  branch: OutlineBranch,
  tick?: number,
  runStarted?: string,
  runsEnded?: string[]
): HistoryOutline {
  const ticks = tick === undefined ? [] : [tick];
  const known = outline.branches.some(b => b.branchId === branch.branchId);
  const grow = (b: OutlineBranch): OutlineBranch => ({
    ...b,
    ownTicks: sortedUnion(b.ownTicks, ticks),
    runSpans:
      tick === undefined
        ? b.runSpans
        : withSpans(b.runSpans, tick, runStarted, runsEnded)
  });
  const branches = known
    ? outline.branches.map(b => (b.branchId === branch.branchId ? grow(b) : b))
    : outline.branches.concat([grow(branch)]);
  const candidate = { ...outline, branches };
  return {
    ...candidate,
    highestTick: highestContiguous(candidate, outline.highestTick)
  };
}

function lineOrRefuse(
  state: INavState,
  branchId: string
): { outline: HistoryOutline } | INavResult {
  if (!state.outline) {
    return refuse(state, 'no_outline', 'the history has not arrived yet');
  }
  if (!state.outline.branches.some(b => b.branchId === branchId)) {
    return refuse(state, 'unknown_branch', `there is no branch ${branchId}`);
  }
  return { outline: state.outline };
}

function showOnLine(
  state: INavState,
  branchId: string,
  tick: number
): INavResult {
  const outline = state.outline as HistoryOutline;
  const at = positionAt(outline, branchId, tick);
  if (!at) {
    return refuse(
      state,
      'no_such_moment',
      'that moment is before the first recorded step on this branch'
    );
  }
  return showAt(state, branchId, at.lastEventTick, 'loading');
}

/** Undefined when the moment is settled; otherwise the answer that refuses. */
function unsettledAnswer(
  state: INavState,
  branchId: string,
  tick: number
): INavResult | undefined {
  const outline = state.outline as HistoryOutline;
  if (!isSettledAt(outline, branchId, tick)) {
    return refuse(
      state,
      'run_in_progress',
      'a cell was still running at this moment; pick a moment after it finished'
    );
  }
  return undefined;
}

function navigate(state: INavState, input: NavInput): INavResult {
  switch (input.type) {
    case 'jumpTo': {
      const l = lineOrRefuse(state, input.branchId);
      return 'outline' in l ? showOnLine(state, input.branchId, input.tick) : l;
    }
    case 'selectBranch': {
      const l = lineOrRefuse(state, input.branchId);
      if (!('outline' in l)) {
        return l;
      }
      const tip = tipOf(l.outline, input.branchId);
      return tip === undefined
        ? refuse(state, 'no_such_moment', 'that branch has no recorded steps')
        : showAt(state, input.branchId, tip, 'loading');
    }
    case 'stepBack':
    case 'stepForward':
    case 'jumpToStart': {
      const outline = state.outline;
      const { branchId, tick } = state.view;
      if (!outline || tick === undefined) {
        return refuse(
          state,
          'no_outline',
          'there is nothing to step through yet'
        );
      }
      let target: number | undefined;
      if (input.type === 'stepBack') {
        target = previousTick(outline, branchId, tick);
        if (target === undefined) {
          return refuse(state, 'at_start', 'this is the first step');
        }
      } else if (input.type === 'stepForward') {
        target = nextTick(outline, branchId, tick);
        if (target === undefined) {
          return refuse(
            state,
            'at_newest',
            'this is the newest step on this branch'
          );
        }
      } else {
        target = lineTicks(outline, branchId)[0];
        if (target === undefined) {
          return refuse(state, 'at_start', 'this branch has no steps');
        }
      }
      return showAt(state, branchId, target, 'loading');
    }
    case 'backToNow': {
      if (!state.outline) {
        return refuse(state, 'no_outline', 'the history has not arrived yet');
      }
      return showAt(state, state.active.branchId, state.active.tip, 'loading');
    }
    case 'continueFromHere': {
      const { outline, view, pending } = state;
      if (!outline || view.tick === undefined) {
        return refuse(
          state,
          'no_such_moment',
          'there is no moment to continue from'
        );
      }
      if (state.connection.status !== 'connected') {
        return refuse(
          state,
          'disconnected',
          'Blaym is reconnecting; try again in a moment'
        );
      }
      if (pending) {
        return refuse(
          state,
          'change_pending',
          'a switch or new branch is already in progress'
        );
      }
      const blocked = unsettledAnswer(state, view.branchId, view.tick);
      if (blocked) {
        return blocked;
      }
      return {
        state: {
          ...state,
          pending: {
            kind: 'create',
            fromBranchId: view.branchId,
            fromTick: view.tick
          }
        },
        requests: [
          clean({
            type: 'createBranch' as const,
            fromBranchId: view.branchId,
            fromTick: view.tick,
            name: input.name
          })
        ]
      };
    }
    case 'switchTo': {
      const l = lineOrRefuse(state, input.branchId);
      if (!('outline' in l)) {
        return l;
      }
      if (state.connection.status !== 'connected') {
        return refuse(
          state,
          'disconnected',
          'Blaym is reconnecting; try again in a moment'
        );
      }
      if (state.pending) {
        return refuse(
          state,
          'change_pending',
          'a switch or new branch is already in progress'
        );
      }
      if (input.branchId === state.active.branchId) {
        return refuse(
          state,
          'already_active',
          'that branch is already the one being recorded'
        );
      }
      const tip = state.active.tip;
      const blocked =
        tip === undefined
          ? undefined
          : unsettledAnswer(state, state.active.branchId, tip);
      if (blocked) {
        return blocked;
      }
      return {
        state: {
          ...state,
          pending: { kind: 'switch', branchId: input.branchId }
        },
        requests: [{ type: 'switchBranch', branchId: input.branchId }]
      };
    }
    case 'outlineReceived': {
      const problems = checkOutline(input.outline);
      if (problems.length) {
        return refuse(
          state,
          'outline_unsound',
          'the history outline broke a rule and was ignored',
          {
            problems
          }
        );
      }
      if (
        state.outline &&
        input.outline.highestTick < state.outline.highestTick
      ) {
        return refuse(
          state,
          'outline_older',
          'an older outline arrived and was ignored'
        );
      }
      if (
        !input.outline.branches.some(b => b.branchId === state.active.branchId)
      ) {
        return refuse(
          state,
          'unknown_active_branch',
          'the outline does not contain the active branch'
        );
      }
      return withOutline(state, input.outline, []);
    }
    case 'historyAdvanced': {
      const o = state.outline;
      const n = input.notification;
      if (!o) {
        return refuse(state, 'no_outline', 'the history has not arrived yet');
      }
      if (n.tick <= o.highestTick) {
        return unchanged(state);
      }
      const ask: NavRequest[] = [
        { type: 'fetchMissedSince', tick: o.highestTick }
      ];
      if (n.tick > o.highestTick + 1) {
        return { state, requests: ask };
      }
      const known = o.branches.find(b => b.branchId === n.branchId);
      if (!known && !n.newBranch) {
        return { state, requests: ask };
      }
      const candidate = withBranch(
        o,
        known ?? (n.newBranch as OutlineBranch),
        n.tick,
        n.runStarted,
        n.runsEnded
      );
      const problems = checkOutline(candidate);
      if (problems.length) {
        return refuse(
          state,
          'outline_unsound',
          'a notification broke a history rule and was ignored',
          {
            problems
          }
        );
      }
      return withOutline(state, candidate, []);
    }
    case 'stateReceived': {
      const { view } = state;
      if (view.awaiting === undefined || input.requestNo !== view.awaiting) {
        return refuse(
          state,
          'stale_answer',
          'that answer was for an earlier request and was dropped'
        );
      }
      if (view.tick === undefined) {
        return refuse(
          state,
          'stale_answer',
          'there is no position to show it at'
        );
      }
      const r = input.result;
      if (
        r.status === 'ok' &&
        (r.state.position.branchId !== view.branchId ||
          r.state.position.tick !== view.tick)
      ) {
        return refuse(
          state,
          'answer_mismatch',
          'the answer is for a different position and was dropped'
        );
      }
      return unchanged({
        ...state,
        view: clean({
          ...view,
          shown: {
            requestNo: input.requestNo,
            branchId: view.branchId,
            tick: view.tick,
            result: r
          },
          freshness: 'current' as Freshness,
          awaiting: undefined
        }) as IView
      });
    }
    case 'branchCreated': {
      if (!state.outline || state.pending?.kind !== 'create') {
        return refuse(
          state,
          'unexpected_answer',
          'no new branch was being created'
        );
      }
      const known = state.outline.branches.some(
        b => b.branchId === input.branch.branchId
      );
      const candidate = known
        ? state.outline
        : withBranch(state.outline, input.branch);
      const problems = checkOutline(candidate);
      if (problems.length) {
        return refuse(
          state,
          'outline_unsound',
          'the new branch broke a history rule and was ignored',
          {
            problems
          }
        );
      }
      const gap: NavRequest[] =
        maxTickOf(candidate) > candidate.highestTick
          ? [{ type: 'fetchMissedSince', tick: candidate.highestTick }]
          : [];
      return adoptActive(state, candidate, input.branch.branchId, gap);
    }
    case 'switched': {
      if (
        !state.outline ||
        state.pending?.kind !== 'switch' ||
        state.pending.branchId !== input.branchId ||
        !state.outline.branches.some(b => b.branchId === input.branchId)
      ) {
        return refuse(
          state,
          'unexpected_answer',
          'no switch to that branch was in progress'
        );
      }
      return adoptActive(state, state.outline, input.branchId, []);
    }
    case 'switchFailed': {
      if (!state.pending) {
        return refuse(
          state,
          'unexpected_answer',
          'no switch or new branch was in progress'
        );
      }
      const next: INavState = { ...state };
      delete next.pending;
      return {
        state: next,
        requests: [],
        refusal: { kind: 'switch_failed', reason: input.reason }
      };
    }
    case 'disconnected': {
      return unchanged({
        ...state,
        connection: { ...state.connection, status: 'reconnecting' }
      });
    }
    case 'reconnected': {
      const connected: INavState = {
        ...state,
        connection: { ...state.connection, status: 'connected' }
      };
      if (!connected.outline) {
        return { state: connected, requests: [{ type: 'fetchOutline' }] };
      }
      const requests: NavRequest[] = [
        { type: 'fetchMissedSince', tick: connected.connection.lastTickSeen }
      ];
      const { view } = connected;
      // An answer that was in flight is lost: ask again under a new number.
      if (view.awaiting !== undefined && view.tick !== undefined) {
        const again = ask(connected, view.branchId, view.tick, view.freshness);
        return {
          state: again.state,
          requests: requests.concat(again.requests)
        };
      }
      return { state: connected, requests };
    }
  }
}

/** The one entry point. Does not change `state`. */
export function navigator(state: INavState, input: NavInput): INavResult {
  return navigate(state, input);
}
