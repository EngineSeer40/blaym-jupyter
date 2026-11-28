/**
 * The mapping between a position (rank of a tick among all ticks) and a
 * distance along the scroll axis, for the tree and the overview strip. Pure
 * and orientation-neutral: names are `along` (the scroll axis), `length` (the
 * window along it) and `up` (newest at the start of the axis, as the tree
 * draws it). No DOM, no pixels of any particular screen.
 */

/** The share of the window, at each end, where following and edge scrolling act. */
export const EDGE = 0.2;
/** Zoom range in pixels per position; the lower end is the fit of the whole history. */
export const ZOOM_MAX = 40;
/** Fastest edge scroll, as a multiple of one step. */
export const EDGE_SPEED_MAX = 1.5;

export interface Viewport {
  /** The number of positions. */
  size: number;
  /** Pixels per position along the axis. */
  step: number;
  /** The window's extent along the axis. */
  length: number;
  /** Space before the first and after the last position. */
  pad: number;
  /** Newest at the start of the axis. */
  up: boolean;
}

/** Where a position sits along the content. */
export const alongOf = (v: Viewport, pos: number): number =>
  v.pad + (v.up ? v.size - 1 - pos : pos) * v.step;

/** The position at a point along the content (fractional). */
export const posOf = (v: Viewport, along: number): number => {
  const k = (along - v.pad) / v.step;
  return v.up ? v.size - 1 - k : k;
};

/** The extent of the whole content. */
export const contentLength = (v: Viewport): number =>
  v.pad * 2 + Math.max(0, v.size - 1) * v.step;

/** Keeps the window inside the content. */
export const clampOff = (v: Viewport, off: number): number =>
  Math.max(0, Math.min(Math.max(0, contentLength(v) - v.length), off));

/** The offset that puts the position at the middle of the window. */
export const centerOff = (v: Viewport, pos: number): number =>
  clampOff(v, alongOf(v, pos) - v.length / 2);

/** The step that fits the whole history in the window (never above the zoom maximum's fit). */
export const fitStep = (size: number, length: number, pad: number): number =>
  Math.max(0.05, (length - pad * 2) / Math.max(1, size - 1));

/** A new step after zooming by `factor`, kept between the fit and the maximum. */
export const zoomStep = (v: Viewport, factor: number): number =>
  Math.max(
    fitStep(v.size, v.length, v.pad),
    Math.min(ZOOM_MAX, v.step * factor)
  );

/** The offset that keeps the position under `anchor` (a point in the window) where it is after the step changes. */
export const offForAnchor = (
  v: Viewport,
  pos: number,
  anchor: number
): number => clampOff(v, alongOf(v, pos) - anchor);

/**
 * Keeps a position inside the inner margins. Off the window altogether it
 * asks for a jump (the caller animates to the returned offset); inside the
 * margin it scrolls just enough.
 */
export function followOff(
  v: Viewport,
  off: number,
  pos: number
): { off: number; jump: boolean } {
  const a = alongOf(v, pos) - off;
  const lo = EDGE * v.length;
  const hi = v.length - lo;
  if (a < 0 || a > v.length) {
    return { off: centerOff(v, pos), jump: true };
  }
  if (a < lo) {
    return { off: clampOff(v, off - (lo - a)), jump: false };
  }
  if (a > hi) {
    return { off: clampOff(v, off + (a - hi)), jump: false };
  }
  return { off, jump: false };
}

/** The edge scroll speed for a pointer at `a` in the window: negative toward the start, faster nearer the edge, 0 inside the margins. */
export function edgeSpeed(length: number, a: number): number {
  const lo = EDGE * length;
  const hi = length - lo;
  const v = a < lo ? -(lo - a) / lo : a > hi ? (a - hi) / lo : 0;
  return Math.max(-EDGE_SPEED_MAX, Math.min(EDGE_SPEED_MAX, v));
}

/** Which end of the window a position is past, or `null` when it is inside. */
export function offScreen(
  v: Viewport,
  off: number,
  pos: number
): 'start' | 'end' | null {
  const a = alongOf(v, pos) - off;
  return a < 0 ? 'start' : a > v.length ? 'end' : null;
}
