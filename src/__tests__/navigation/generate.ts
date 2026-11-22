import * as fc from 'fast-check';
import {
  initialState,
  INavResult,
  INavState,
  NavInput
} from '../../navigation';
import { deepFreeze } from './scenario';
import {
  buildHistory,
  IOp,
  lineEvents,
  opsArb,
  outlineOfHistory
} from './outline-gen';
import { maxTickOf, SimulatedServer } from './simulated-server';

/**
 * Generated interleaved sequences for the navigator: history growth, user
 * inputs and delivery problems. An action is plain data (a kind and three
 * random numbers); the runner decides what it means against the world as it is
 * at that moment, so shrinking stays meaningful.
 */

export type ActionKind =
  | 'grow'
  | 'growBranch'
  | 'jumpTo'
  | 'stepBack'
  | 'stepForward'
  | 'jumpToStart'
  | 'backToNow'
  | 'selectBranch'
  | 'continueFromHere'
  | 'switchTo'
  | 'serve'
  | 'deliver'
  | 'flush'
  | 'duplicate'
  | 'duplicateAnswer'
  | 'drop'
  | 'disconnect'
  | 'reconnect';

export interface IAction {
  kind: ActionKind;
  a: number;
  b: number;
  c: number;
}

const WEIGHTS: Array<[ActionKind, number]> = [
  ['grow', 10],
  ['growBranch', 2],
  ['jumpTo', 6],
  ['stepBack', 6],
  ['stepForward', 5],
  ['jumpToStart', 2],
  ['backToNow', 5],
  ['selectBranch', 4],
  ['continueFromHere', 8],
  ['switchTo', 6],
  ['serve', 14],
  ['deliver', 18],
  ['flush', 8],
  ['duplicate', 3],
  ['duplicateAnswer', 3],
  ['drop', 3],
  ['disconnect', 2],
  ['reconnect', 3]
];

const KINDS: ActionKind[] = [];
WEIGHTS.forEach(([k, w]) => {
  for (let n = 0; n < w; n++) {
    KINDS.push(k);
  }
});

export const actionsArb: fc.Arbitrary<IAction[]> = fc.array(
  fc.record({
    kind: fc.constantFrom(...KINDS),
    a: fc.nat(1000),
    b: fc.nat(1000),
    c: fc.nat(1000)
  }),
  { minLength: 15, maxLength: 80, size: 'max' }
);

export interface ISequence {
  ops: IOp[];
  actions: IAction[];
}

export const sequenceArb: fc.Arbitrary<ISequence> = fc.record({
  ops: opsArb,
  actions: actionsArb
});

/** Structural equality that also tells apart a missing key from an undefined one. */
export function isDeepStrictEqual(a: unknown, b: unknown): boolean {
  if (a === b) {
    return true;
  }
  if (typeof a !== 'object' || typeof b !== 'object' || !a || !b) {
    return false;
  }
  if (Array.isArray(a) !== Array.isArray(b)) {
    return false;
  }
  const ka = Object.keys(a).sort();
  const kb = Object.keys(b).sort();
  return (
    ka.length === kb.length &&
    ka.every(
      (k, i) =>
        k === kb[i] &&
        isDeepStrictEqual(
          (a as Record<string, unknown>)[k],
          (b as Record<string, unknown>)[k]
        )
    )
  );
}

export type Navigate = (state: INavState, input: NavInput) => INavResult;

/** One step of the run, handed to the property checks. */
export interface IStep {
  index: number;
  input: NavInput;
  prev: INavState;
  next: INavState;
  result: INavResult;
  server: SimulatedServer;
  /** The highest fetchState request number issued so far (after this step). */
  latestIssued: number;
  /** The active tip when the current looking-back began (while looking back). */
  lookBackFrom: number;
  /** A backToNow was accepted and no other move has been made since. */
  backToNowArmed: boolean;
}

export type StepCheck = (step: IStep) => void;

const USER_INPUTS = new Set<string>([
  'jumpTo',
  'stepBack',
  'stepForward',
  'jumpToStart',
  'selectBranch'
]);

/**
 * Runs a sequence against `navigate`, calling `check` after every step. A
 * failure is rethrown with the most recent steps, so the shrunk case reads as a
 * short story.
 */
export function runSequence(
  seq: ISequence,
  navigate: Navigate,
  check: StepCheck,
  deterministic = false
): void {
  const server = new SimulatedServer(buildHistory(seq.ops));
  let state: INavState = deepFreeze(initialState('b0'));
  let index = 0;
  let latestIssued = 0;
  let lookBackFrom = 0;
  let armed = false;
  const trace: string[] = [];

  const apply = (input: NavInput): void => {
    const prev = state;
    const result = navigate(prev, deepFreeze(input));
    if (deterministic) {
      const again = navigate(prev, input);
      if (!isDeepStrictEqual(again, result)) {
        throw new Error(
          'the navigator gave different outputs for the same input'
        );
      }
    }
    state = deepFreeze(result.state);
    index += 1;
    for (const r of result.requests) {
      if (r.type === 'fetchState') {
        latestIssued = Math.max(latestIssued, r.requestNo);
      }
      const reliable = r.type === 'createBranch' || r.type === 'switchBranch';
      if (reliable || server.connected) {
        server.toServer.push({ request: r, reliable });
      }
    }
    if (prev.view.mode === 'following' && state.view.mode === 'lookingBack') {
      lookBackFrom = prev.active.tip ?? 0;
    }
    if (input.type === 'backToNow' && !result.refusal) {
      armed = true;
    } else if (
      USER_INPUTS.has(input.type) ||
      input.type === 'switched' ||
      input.type === 'branchCreated'
    ) {
      armed = false;
    }
    trace.push(
      `#${index} ${JSON.stringify(input)} -> view ${state.view.branchId}@${state.view.tick} ${state.view.mode}/${state.view.freshness}, active ${state.active.branchId}@${state.active.tip}` +
        (result.refusal ? `, refused: ${result.refusal.kind}` : '') +
        (result.requests.length
          ? `, asks ${JSON.stringify(result.requests)}`
          : '')
    );
    try {
      check({
        index,
        input,
        prev,
        next: state,
        result,
        server,
        latestIssued,
        lookBackFrom,
        backToNowArmed: armed
      });
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      throw new Error(
        `${message}\nlast steps:\n${trace.slice(-25).join('\n')}`
      );
    }
    if (state.view.freshness === 'current') {
      armed = false;
    }
  };

  const serveAt = (i: number, b: number, c: number): void => {
    const [item] = server.toServer.splice(i, 1);
    server.serve(item.request, b, c);
  };

  const deliverAt = (i: number, keep: boolean): void => {
    const item = keep ? server.toClient[i] : server.toClient.splice(i, 1)[0];
    apply(item.input);
  };

  const flush = (b: number, c: number): void => {
    for (let n = 0; n < 60; n++) {
      if (server.toServer.length) {
        serveAt(0, b, c);
      } else if (server.toClient.length) {
        deliverAt(0, false);
      } else {
        return;
      }
    }
  };

  const branchPick = (a: number): string => {
    const known =
      state.outline && a % 7 !== 0 ? state.outline.branches : server.h.branches;
    return known[a % known.length].branchId;
  };

  const growTarget = (a: number): string =>
    a % 5 < 3
      ? server.active
      : server.h.branches[Math.floor(a / 5) % server.h.branches.length]
          .branchId;

  try {
    apply({ type: 'outlineReceived', outline: outlineOfHistory(server.h) });
    flush(1, 1);

    for (const act of seq.actions) {
      const { a, b, c } = act;
      switch (act.kind) {
        case 'grow':
          server.announce(server.addEvent(growTarget(a), b, c));
          break;
        case 'growBranch':
          server.announce(server.addBranch(growTarget(a), c));
          break;
        case 'jumpTo': {
          const branchId = branchPick(a);
          const ticks = lineEvents(server.h, branchId).map(e => e.tick);
          const tick =
            b % 5 === 0
              ? c % (maxTickOf(server.h) + 3)
              : ticks[c % ticks.length];
          apply({ type: 'jumpTo', branchId, tick });
          break;
        }
        case 'stepBack':
        case 'stepForward':
        case 'jumpToStart':
        case 'backToNow':
          apply({ type: act.kind });
          break;
        case 'selectBranch':
        case 'switchTo':
          apply({ type: act.kind, branchId: branchPick(a) });
          break;
        case 'continueFromHere':
          apply({ type: 'continueFromHere' });
          break;
        case 'serve': {
          const ok = server.toServer
            .map((m, i) => (m.reliable || server.connected ? i : -1))
            .filter(i => i >= 0);
          if (ok.length) {
            serveAt(ok[a % ok.length], b, c);
          }
          break;
        }
        case 'deliver':
        case 'duplicate': {
          const ok = server.toClient
            .map((m, i) => (m.reliable || server.connected ? i : -1))
            .filter(i => i >= 0);
          if (ok.length) {
            deliverAt(ok[a % ok.length], act.kind === 'duplicate');
          }
          break;
        }
        case 'duplicateAnswer': {
          // A switch or branch answer delivered a second time (kept in the queue).
          const ok = server.toClient
            .map((m, i) => (m.reliable ? i : -1))
            .filter(i => i >= 0);
          if (ok.length) {
            deliverAt(ok[a % ok.length], true);
          }
          break;
        }
        case 'drop': {
          const ok = server.toClient.map((m, i) => (m.reliable ? -1 : i));
          const lossy = ok.filter(i => i >= 0);
          if (lossy.length) {
            server.toClient.splice(lossy[a % lossy.length], 1);
          }
          break;
        }
        case 'flush':
          flush(b, c);
          break;
        case 'disconnect':
          if (server.connected) {
            server.connected = false;
            server.toClient = server.toClient.filter(m => m.reliable);
            server.toServer = server.toServer.filter(m => m.reliable);
            apply({ type: 'disconnected' });
          }
          break;
        case 'reconnect':
          if (!server.connected) {
            server.connected = true;
            apply({ type: 'reconnected' });
          }
          break;
      }
    }
    flush(1, 1);
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    throw e instanceof Error && message.includes('last steps:')
      ? e
      : new Error(`${message}\nlast steps:\n${trace.slice(-25).join('\n')}`);
  }
}
