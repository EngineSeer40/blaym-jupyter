/**
 * The viewer's loop: owns the navigator state, carries out its requests
 * through the client, feeds stream frames and connection changes back in, and
 * keeps the focus position equal to the navigator's view. The navigator
 * itself stays pure. S8's switching commands are not wired here.
 */
import { initialState, navigator } from '../navigation';
import type { INavState, IView, NavInput, NavRequest } from '../navigation';
import type { Cancel } from '../recording/types';
import type { Client } from './client';
import { createFocus } from './focus';
import type { IFocusStore } from './focus';

export interface SessionOptions {
  client: Client;
  explorationId: string;
  /** The branch recording goes to, until the outline says otherwise. Empty: the outline's root branch. */
  activeBranchId: string;
  /** Hears a failed read. The navigator keeps showing the last good answer. */
  onError?: (error: unknown, request: NavRequest) => void;
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
    const result = navigator(nav, input);
    nav = result.state;
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
        default:
          // createBranch and switchBranch belong to S8.
          return;
      }
    } catch (error) {
      o.onError?.(error, request);
    }
  }

  return {
    focus: owner.store,
    branch: () => branch,
    view: () => nav.view,
    connection: () => nav.connection.status,
    activeBranchId: () => nav.active.branchId,
    activeTip: () => nav.active.tip,
    backToNow: () => feed({ type: 'backToNow' }),
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
