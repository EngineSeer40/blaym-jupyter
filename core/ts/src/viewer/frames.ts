/**
 * Quick frame or full frame. While the playhead is moving (a drag, Play, key
 * steps) the ghost book draws a quick frame from each newest answer: code and
 * structure, outputs as sized placeholders, code typing in toward the new
 * version. The view is settled when the pointer is released or nothing has
 * moved for `SETTLE_MS`; then the full frame is drawn. The viewer never applies
 * events itself: a quick frame is only the newest answer drawn lightly.
 */
import type { OutputItem } from 'blaym-notebook-types';

/** No move for this long and the view is settled. */
export const SETTLE_MS = 250;
/** Typing pace in characters a second (owner answer 3). */
export const TYPE_PACE = 120;
/** Typing that is further behind than this skips ahead. */
export const SKIP_LAG_MS = 400;

export interface Frames {
  /** The playhead moved: the view is moving until `SETTLE_MS` passes without another call. */
  moving(): void;
  /** The pointer was released: settled at once. */
  release(): void;
  isMoving(): boolean;
  /** Calls `fn(moving)` when the state changes. Returns the unsubscribe function. */
  subscribe(fn: (moving: boolean) => void): () => void;
}

export interface FramesOptions {
  /** For the harness: stays moving, so a quick frame can be shot. */
  hold?: boolean;
  setTimeout?: (fn: () => void, ms: number) => () => void;
}

export function createFrames(options: FramesOptions = {}): Frames {
  const later =
    options.setTimeout ??
    ((fn: () => void, ms: number) => {
      const handle = window.setTimeout(fn, ms);
      return () => window.clearTimeout(handle);
    });
  const subscribers = new Set<(moving: boolean) => void>();
  let moving = !!options.hold;
  let cancel: (() => void) | undefined;

  const set = (next: boolean) => {
    if (next !== moving) {
      moving = next;
      for (const fn of Array.from(subscribers)) {
        fn(moving);
      }
    }
  };
  return {
    moving() {
      cancel?.();
      cancel = undefined;
      set(true);
      if (!options.hold) {
        cancel = later(() => {
          cancel = undefined;
          set(false);
        }, SETTLE_MS);
      }
    },
    release() {
      cancel?.();
      cancel = undefined;
      if (!options.hold) {
        set(false);
      }
    },
    isMoving: () => moving,
    subscribe(fn) {
      subscribers.add(fn);
      return () => {
        subscribers.delete(fn);
      };
    }
  };
}

/**
 * The code on show `elapsedMs` after typing began from `from` toward `to`: the
 * common start stays, the rest is typed at `pace`, and when typing is more than
 * `lagMs` behind it skips ahead to that much remaining.
 */
export function typedText(
  from: string,
  to: string,
  elapsedMs: number,
  pace = TYPE_PACE,
  lagMs = SKIP_LAG_MS
): string {
  let keep = 0;
  while (keep < from.length && keep < to.length && from[keep] === to[keep]) {
    keep++;
  }
  const rest = to.length - keep;
  const typed = Math.min(
    rest,
    Math.max(
      Math.floor((elapsedMs * pace) / 1000),
      Math.ceil(rest - (lagMs * pace) / 1000)
    )
  );
  return to.slice(0, keep + Math.max(0, typed));
}

/** What a placeholder stands for, by the kinds of data the output holds. */
export function placeholderKind(item: OutputItem): 'chart' | 'table' | 'other' {
  const mimes = item.mimeTypes;
  return mimes.some(m => m.startsWith('image/') || /plotly|chart/.test(m))
    ? 'chart'
    : mimes.includes('text/html')
      ? 'table'
      : 'other';
}

const DEFAULT_HEIGHT = { chart: 240, table: 120, other: 56 };

/** The height last drawn for an output, by content hash; the kind's default until one is known. */
export interface SizeMemory {
  remember(contentHash: string, height: number): void;
  heightOf(contentHash: string, kind?: 'chart' | 'table' | 'other'): number;
  /** The height last drawn, or undefined when none has been. */
  known(contentHash: string): number | undefined;
}

export function createSizeMemory(): SizeMemory {
  const seen = new Map<string, number>();
  return {
    remember(contentHash, height) {
      if (height > 0) {
        seen.set(contentHash, height);
      }
    },
    heightOf: (contentHash, kind = 'other') =>
      seen.get(contentHash) ?? DEFAULT_HEIGHT[kind],
    known: contentHash => seen.get(contentHash)
  };
}
