import type {
  HistoryAdvanced,
  HistoryOutline,
  OutlineBranch,
  RebuildResult
} from 'blaym-notebook-types';
import type { IOutlineProblem } from './line';

/**
 * Navigator state, inputs, requests and refusals. These are internal to the
 * navigation logic; they are not contracts and never cross the wire.
 */

/** The answer the view is showing, and the request it answered. */
export interface IShownAnswer {
  requestNo: number;
  branchId: string;
  tick: number;
  result: RebuildResult;
}

/**
 * `current`: the shown answer is for the view's position.
 * `loading`: the user moved; the shown answer (if any) is for an earlier position.
 * `catchingUp`: following now and the tip moved; the shown answer is behind it.
 */
export type Freshness = 'current' | 'loading' | 'catchingUp';

/** What the user is looking at. Replaced as a whole on every change. */
export interface IView {
  mode: 'following' | 'lookingBack';
  /** The viewed branch. Looking back never changes it. */
  branchId: string;
  /** A tick on the viewed branch's line; undefined only before any event is known. */
  tick?: number;
  shown?: IShownAnswer;
  freshness: Freshness;
  /** The number of the one request whose answer will be shown. */
  awaiting?: number;
  /** The active branch's tip when the user started looking back. */
  lookingBackFromTip?: number;
}

/** The branch recording goes to. Only a server answer changes it. */
export interface IActive {
  branchId: string;
  tip?: number;
}

/** At most one outstanding switch or branch creation. */
export interface IPending {
  kind: 'switch' | 'create';
  /** The branch being switched to (switch only). */
  branchId?: string;
  fromBranchId?: string;
  fromTick?: number;
}

export interface IConnection {
  status: 'connected' | 'reconnecting';
  /** The highest contiguous tick the navigator knows it has seen. */
  lastTickSeen: number;
}

export interface INavState {
  outline?: HistoryOutline;
  active: IActive;
  view: IView;
  /** New ticks on the active line since the user started looking back. */
  newSinceLookingBack: number;
  pending?: IPending;
  connection: IConnection;
  lastRequestNo: number;
}

export type NavInput =
  | { type: 'jumpTo'; branchId: string; tick: number }
  | { type: 'stepBack' }
  | { type: 'stepForward' }
  | { type: 'jumpToStart' }
  | { type: 'backToNow' }
  | { type: 'selectBranch'; branchId: string }
  | { type: 'continueFromHere'; name?: string }
  | { type: 'switchTo'; branchId: string }
  | { type: 'outlineReceived'; outline: HistoryOutline }
  | { type: 'historyAdvanced'; notification: HistoryAdvanced }
  | { type: 'stateReceived'; requestNo: number; result: RebuildResult }
  | { type: 'branchCreated'; branch: OutlineBranch }
  | { type: 'switched'; branchId: string }
  | { type: 'switchFailed'; reason: string }
  | { type: 'disconnected' }
  | { type: 'reconnected' };

export type NavRequest =
  | { type: 'fetchState'; requestNo: number; branchId: string; tick: number }
  | { type: 'fetchOutline' }
  | { type: 'fetchMissedSince'; tick: number }
  | {
      type: 'createBranch';
      fromBranchId: string;
      fromTick: number;
      name?: string;
    }
  | { type: 'switchBranch'; branchId: string };

export type RefusalKind =
  | 'no_outline'
  | 'unknown_branch'
  | 'no_such_moment'
  | 'at_start'
  | 'at_newest'
  | 'run_in_progress'
  | 'change_pending'
  | 'disconnected'
  | 'already_active'
  | 'unexpected_answer'
  | 'stale_answer'
  | 'answer_mismatch'
  | 'outline_unsound'
  | 'outline_older'
  | 'unknown_active_branch'
  | 'switch_failed';

/** An input that did not take effect, with a plain reason. Never thrown. */
export interface IRefusal {
  kind: RefusalKind;
  reason: string;
  problems?: IOutlineProblem[];
}

export interface INavResult {
  state: INavState;
  requests: NavRequest[];
  refusal?: IRefusal;
}
