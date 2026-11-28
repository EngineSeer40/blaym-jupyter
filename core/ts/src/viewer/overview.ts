/**
 * The overview strip beside the tree: the whole history squeezed into the
 * strip as a density ribbon per lane (scaled against the busiest bin), lane
 * lines, the playhead and a draggable box for the part in the window. A click
 * away from the box asks to centre there (the caller animates it); a drag
 * moves the window at once. Vertical only in this version, newest at the top.
 */
import { alongOf, posOf } from './treeview';
import type { Viewport } from './treeview';
import type { TreeLayout } from './treelayout';

const SVG_NS = 'http://www.w3.org/2000/svg';
const LANE_W = 8;
const ORIGIN = 2;

export interface OverviewInput {
  layout: TreeLayout;
  /** The selected branch's lane is drawn in the highlight. */
  selectedBranchId: string;
  /** The playhead's position, if there is one. */
  headPos?: number;
  /** The positions at the two ends of the tree's window. */
  window: [number, number];
}

export interface Overview {
  draw(input: OverviewInput): void;
  /** Called with the position to centre on and whether to animate (a far click) or move at once (a drag). */
  onPick(fn: (pos: number, animate: boolean) => void): void;
}

const rect = (attrs: Record<string, string | number>, cls: string) => {
  const e = document.createElementNS(SVG_NS, 'rect');
  for (const [k, v] of Object.entries(attrs)) {
    e.setAttribute(k, String(v));
  }
  e.setAttribute('class', cls);
  return e;
};

/** The count of markers per lane in each of `bins` bins along the strip, and the largest count. */
export function densityOf(
  layout: TreeLayout,
  v: Viewport,
  bins: number
): { counts: Map<number, Map<number, number>>; max: number } {
  const counts = new Map<number, Map<number, number>>();
  let max = 1;
  for (const m of layout.markers) {
    const bin = Math.min(
      bins - 1,
      Math.max(0, Math.floor(alongOf(v, m.pos) - v.pad))
    );
    const lane = counts.get(m.lane) ?? new Map<number, number>();
    const n = (lane.get(bin) ?? 0) + 1;
    lane.set(bin, n);
    counts.set(m.lane, lane);
    max = Math.max(max, n);
  }
  return { counts, max };
}

export function createOverview(host: HTMLElement): Overview {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'bv-ov-svg');
  svg.setAttribute('width', '100%');
  const box = document.createElement('div');
  box.className = 'bv-ov-box';
  const head = document.createElement('div');
  head.className = 'bv-ov-head';
  host.replaceChildren(svg, box, head);

  let picked: ((pos: number, animate: boolean) => void) | undefined;
  let last: OverviewInput | undefined;
  let key = '';
  let drawn: TreeLayout | undefined;
  let grab = 0;
  let dragging = false;

  const view = (size: number): Viewport => {
    const length = host.clientHeight || 1;
    return { size, step: length / Math.max(1, size), length, pad: 0, up: true };
  };

  function ribbon(input: OverviewInput, v: Viewport): void {
    const { layout } = input;
    const bins = Math.max(1, Math.round(v.length));
    const { counts, max } = densityOf(layout, v, bins);
    const out: SVGElement[] = [];
    const laneW = Math.max(
      2,
      Math.min(
        LANE_W,
        Math.floor(((host.clientWidth || 18) - ORIGIN * 2) / layout.laneCount)
      )
    );
    for (const l of layout.lanes) {
      const x = ORIGIN + l.lane * laneW;
      const sel = l.branchId === input.selectedBranchId;
      const y0 = alongOf(v, l.lastPos);
      const y1 = alongOf(v, l.firstPos);
      out.push(
        rect(
          { x: x + laneW / 2 - 0.5, y: y0, width: 1, height: y1 - y0 + 1 },
          `bv-ov-lane${sel ? ' bv-ov-sel' : ''}`
        )
      );
      for (const [bin, n] of counts.get(l.lane) ?? []) {
        // The same branch owns a lane's whole run, so the bin takes the lane's colour.
        out.push(
          rect(
            {
              x,
              y: bin,
              width: laneW - 1,
              height: 1,
              opacity: (0.25 + 0.75 * Math.sqrt(n / max)).toFixed(2)
            },
            `bv-ov-dense${sel ? ' bv-ov-sel' : ''}`
          )
        );
      }
    }
    svg.replaceChildren(...out);
  }

  function place(input: OverviewInput, v: Viewport): void {
    const a = alongOf(v, input.window[0]);
    const b = alongOf(v, input.window[1]);
    const top = Math.max(0, Math.min(a, b));
    const bottom = Math.min(v.length, Math.max(a, b));
    box.style.setProperty('top', `${top}px`);
    box.style.setProperty('height', `${Math.max(4, bottom - top)}px`);
    head.hidden = input.headPos === undefined;
    if (input.headPos !== undefined) {
      head.style.setProperty('top', `${alongOf(v, input.headPos + 0.5)}px`);
    }
  }

  function pick(ev: PointerEvent, first: boolean): void {
    if (!last || !picked) {
      return;
    }
    const v = view(last.layout.size);
    const pos = posOf(v, ev.clientY - host.getBoundingClientRect().top) - 0.5;
    if (first) {
      const mid = (last.window[0] + last.window[1]) / 2;
      const half = Math.abs(last.window[0] - last.window[1]) / 2;
      grab = Math.abs(pos - mid) <= half ? pos - mid : 0;
    }
    picked(pos - grab, first && grab === 0);
  }

  host.addEventListener('pointerdown', ev => {
    dragging = true;
    host.setPointerCapture(ev.pointerId);
    pick(ev, true);
  });
  host.addEventListener('pointermove', ev => {
    if (dragging) {
      pick(ev, false);
    }
  });
  const stop = () => {
    dragging = false;
  };
  host.addEventListener('pointerup', stop);
  host.addEventListener('pointercancel', stop);

  return {
    draw(input) {
      last = input;
      if (input.layout.size === 0) {
        svg.replaceChildren();
        return;
      }
      const v = view(input.layout.size);
      const k = [
        input.layout.size,
        input.layout.lanes.length,
        v.length,
        host.clientWidth,
        input.selectedBranchId
      ].join();
      if (k !== key || drawn !== input.layout) {
        key = k;
        drawn = input.layout;
        ribbon(input, v);
      }
      place(input, v);
    },
    onPick(fn) {
      picked = fn;
    }
  };
}
