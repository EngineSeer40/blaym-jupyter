/**
 * The one shared focus state: where the viewer is, what is hovered and
 * selected, which cell is focused and what a diff is shown against. The state
 * is replaced as a whole on every change; parts read it through `subscribe`
 * and change it only through the commands.
 */

/** What a bar item or version-list entry stands for. */
export interface IFocusItem {
  firstTick: number;
  lastTick: number;
  kind: string;
}

export type DiffBase = 'none' | 'now' | 'previous' | { tick: number };

export interface IFocusState {
  position: { branchId: string; tick?: number };
  hovered: IFocusItem | null;
  selected: IFocusItem | null;
  focusedCell: string | null;
  diffBase: DiffBase;
}

export interface IFocusStore {
  get(): IFocusState;
  /** Calls `fn` after every change. Returns the unsubscribe function. */
  subscribe(fn: (state: IFocusState) => void): () => void;
  /** Asks to move; the position itself changes when the session has moved. */
  goto(branchId: string, tick: number): void;
  select(item: IFocusItem | null): void;
  hover(item: IFocusItem | null): void;
  focusCell(cellId: string | null): void;
  showDiff(base: DiffBase): void;
}

/** The session's way to set `position`: not a command, so no part can use it by mistake. */
export interface IFocusOwner {
  store: IFocusStore;
  setPosition(branchId: string, tick?: number): void;
}

export function createFocus(
  branchId: string,
  onGoto: (branchId: string, tick: number) => void
): IFocusOwner {
  let state: IFocusState = {
    position: { branchId },
    hovered: null,
    selected: null,
    focusedCell: null,
    diffBase: 'none'
  };
  const subscribers = new Set<(state: IFocusState) => void>();

  const replace = (next: IFocusState) => {
    state = next;
    for (const fn of Array.from(subscribers)) {
      fn(state);
    }
  };

  const store: IFocusStore = {
    get: () => state,
    subscribe(fn) {
      subscribers.add(fn);
      return () => {
        subscribers.delete(fn);
      };
    },
    goto: onGoto,
    select: selected => replace({ ...state, selected }),
    hover: hovered => replace({ ...state, hovered }),
    focusCell: focusedCell => replace({ ...state, focusedCell }),
    showDiff: diffBase => replace({ ...state, diffBase })
  };

  return {
    store,
    setPosition(b, tick) {
      const p = state.position;
      if (p.branchId !== b || p.tick !== tick) {
        replace({
          ...state,
          position: tick === undefined ? { branchId: b } : { branchId: b, tick }
        });
      }
    }
  };
}
