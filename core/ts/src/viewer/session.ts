/**
 * The viewer's loop: owns the navigator state, carries out its requests
 * through the client, feeds stream frames and connection changes back in, and
 * keeps the focus position equal to the navigator's view. The navigator
 * itself stays pure. Creating a branch goes through the client; the switch
 * itself is the host's (`blaym:switch`), because the page that holds the
 * notebook replaces its cells.
 */
import { initialState, navigator } from '../navigation';
import type { INavState, IView, NavInput, NavRequest } from '../navigation';
import type { Cancel } from '../recording/types';
import type { Client } from './client';
import type { SwitchAnswer } from './host';
import { createFocus } from './focus';
import type { IFocusStore } from './focus';

export interface SessionOptions {
  client: Client;
  explorationId: string;
  /** The branch recording goes to, until the outline says otherwise. Empty: the outline's root branch. */
  activeBranchId: string;
  /** Hears a failed read. The navigator keeps showing the last good answer. */
  onError?: (error: unknown, request: NavRequest) => void;
  /** Asks the host to switch the notebook to a branch. Absent: switching is unavailable here. */
  switchHost?: (branchId: string) => Promise<SwitchAnswer>;
}

export interface Session {
  focus: IFocusStore;
  /** The navigator's view: the shown answer, its freshness, the mode. */
  view(): IView;
  /** The branch recording goes to ('' before the outline is known). */
  activeBranchId(): string;
  /** The active branch's tip tick, once known. */
  activeTip(): number | undefined;
  /** The stream's state as the navigator holds it. */
  connection(): INavState['connection']['status'];
  /** Makes a branch at the viewed moment, then switches the notebook to it. */
  continueFromHere(): void;
  /** Switches the notebook to a branch, from its tip. */
  switchTo(branchId: string): void;
  /** The plain reason "continue from here" is refused right now (the moment is unsettled, or a change is under way); undefined when it would go ahead. */
  continueBlocked(): string | undefined;
  /** True while a branch is being made or the notebook is being switched. */
  switching(): boolean;
  /** The branch the notebook was on before the last switch: the return point. */
  previousBranchId(): string | undefined;
  /** Hides the shown problem. */
  dismissProblem(): void;
  /** The plain reason the last continue or switch was refused or failed; cleared when the next one starts. */
  problem(): string | undefined;
  /** Returns to following the active branch's tip. */
  backToNow(): void;
  /** Calls `fn` whenever the view or the connection state changes. Returns the unsubscribe function. */
  subscribe(fn: (view: IView) => void): () => void;
  /** Resolves to the active branch once it is known: at once when given, else after the first outline. */
  branch(): Promise<string>;
  /** Fetches the outline and starts following the stream. */
  start(): void;
  stop(): void;
}

export function createSession(o: SessionOptions): Session {
  let nav: INavState = initialState(o.activeBranchId);
  const subscribers = new Set<(view: IView) => void>();
  let stopStream: Cancel | undefined;
  let stopped = false;
  let known: (branchId: string) => void = () => undefined;
  const branch = new Promise<string>(resolve => {
    known = resolve;
  });
  if (o.activeBranchId) {
    known(o.activeBranchId);
  }

  let problem: string | undefined;
  let previous: string | undefined;

  const owner = createFocus(o.activeBranchId, (branchId, tick) =>
    feed({ type: 'jumpTo', branchId, tick })
  );

  function feed(input: NavInput): void {
    if (stopped) {
      return;
    }
    const before = nav.view;
    const beforeStatus = nav.connection.status;
    const beforeTip = nav.active.tip;
    const beforeBranch = nav.active.branchId;
    const beforePending = nav.pending;
    const beforeProblem = problem;
    const result = navigator(nav, input);
    nav = result.state;
    if (input.type === 'continueFromHere' || input.type === 'switchTo') {
      problem = undefined;
    }
    if (
      result.refusal &&
      (input.type === 'continueFromHere' ||
        input.type === 'switchTo' ||
        result.refusal.kind === 'switch_failed')
    ) {
      problem = result.refusal.reason;
    }
    if (
      (input.type === 'switched' &&
        beforeBranch &&
        nav.active.branchId !== beforeBranch) ||
      nav.pending !== beforePending ||
      problem !== beforeProblem
    ) {
      previous = beforeBranch;
    }
    if (nav.view.tick !== undefined || nav.view.branchId !== before.branchId) {
      owner.setPosition(nav.view.branchId, nav.view.tick);
    }
    if (
      nav.view !== before ||
      nav.connection.status !== beforeStatus ||
      nav.active.tip !== beforeTip ||
      nav.active.branchId !== beforeBranch
    ) {
      for (const fn of Array.from(subscribers)) {
        fn(nav.view);
      }
    }
    for (const request of result.requests) {
      void run(request);
    }
  }

  /** Carries out one request. A catch-up feeds the answer's outline. */
  async function run(request: NavRequest): Promise<void> {
    try {
      switch (request.type) {
        case 'fetchState': {
          const result = await o.client.state({
            explorationId: o.explorationId,
            branchId: request.branchId,
            tick: request.tick
          });
          feed({ type: 'stateReceived', requestNo: request.requestNo, result });
          return;
        }
        case 'fetchOutline': {
          const outline = await o.client.outline({
            explorationId: o.explorationId
          });
          if (!nav.active.branchId) {
            const root = outline.rootBranchId;
            nav = { ...initialState(root), connection: nav.connection };
            owner.setPosition(root);
            known(root);
          }
          feed({ type: 'outlineReceived', outline });
          return;
        }
        case 'fetchMissedSince': {
          const answer = await o.client.eventsSince({
            explorationId: o.explorationId,
            branchId: nav.active.branchId,
            afterTick: request.tick
          });
          feed({ type: 'outlineReceived', outline: answer.outline });
          return;
        }
        case 'createBranch': {
          const made = await o.client.createBranch({
            explorationId: o.explorationId,
            fromBranchId: request.fromBranchId,
            fromTick: request.fromTick,
            name: request.name ?? autoName()
          });
          if (made.status !== 'ok') {
            feed({ type: 'switchFailed', reason: made.detail });
            return;
          }
          // The navigator then asks for the switch itself.
          feed({ type: 'branchCreated', branch: made.branch });
          return;
        }
        case 'switchBranch': {
          const answer = await askHost(request.branchId);
          if (answer.status === 'ok') {
            feed({ type: 'switched', branchId: request.branchId });
            await refreshOutline();
          } else {
            feed({
              type: 'switchFailed',
              reason: answer.reason ?? 'the notebook could not be switched'
            });
          }
          return;
        }
      }
    } catch (error) {
      o.onError?.(error, request);
      if (request.type === 'createBranch' || request.type === 'switchBranch') {
        feed({
          type: 'switchFailed',
          reason: 'Blaym could not be reached; nothing was changed'
        });
      }
    }
  }

  /** The automatic name; renaming comes later. */
  function autoName(): string {
    return `branch from ${new Date().toTimeString().slice(0, 5)}`;
  }

  async function askHost(branchId: string): Promise<SwitchAnswer> {
    return o.switchHost
      ? o.switchHost(branchId)
      : {
          status: 'failed',
          reason:
            'Switching branches needs JupyterLab; it is not available here'
        };
  }

  async function refreshOutline(): Promise<void> {
    const outline = await o.client.outline({ explorationId: o.explorationId });
    feed({ type: 'outlineReceived', outline });
  }

  return {
    focus: owner.store,
    branch: () => branch,
    view: () => nav.view,
    connection: () => nav.connection.status,
    activeBranchId: () => nav.active.branchId,
    activeTip: () => nav.active.tip,
    backToNow: () => feed({ type: 'backToNow' }),
    continueFromHere: () => feed({ type: 'continueFromHere' }),
    switchTo: branchId => feed({ type: 'switchTo', branchId }),
    problem: () => problem,
    continueBlocked: () =>
      navigator(nav, { type: 'continueFromHere' }).refusal?.reason,
    switching: () => nav.pending !== undefined,
    previousBranchId: () => previous,
    dismissProblem() {
      if (problem !== undefined) {
        problem = undefined;
        for (const fn of Array.from(subscribers)) {
          fn(nav.view);
        }
      }
    },
    subscribe(fn) {
      subscribers.add(fn);
      return () => {
        subscribers.delete(fn);
      };
    },
    start() {
      void run({ type: 'fetchOutline' });
      stopStream = o.client.stream(o.explorationId, undefined, {
        onAdvanced: advanced => {
          const seen = nav.connection.lastTickSeen;
          feed({ type: 'historyAdvanced', notification: advanced });
          // A jump in ticks: never guess. The navigator asks for the catch-up
          // itself; the outline is refetched here as well.
          if (advanced.tick > seen + 1) {
            void run({ type: 'fetchOutline' });
          }
        },
        onDisconnected: () => feed({ type: 'disconnected' }),
        onReconnected: () => {
          feed({ type: 'reconnected' });
          void run({ type: 'fetchOutline' });
        }
      });
    },
    stop() {
      stopped = true;
      stopStream?.();
    }
  };
}
