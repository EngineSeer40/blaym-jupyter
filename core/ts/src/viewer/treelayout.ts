/**
 * The tree's layout: pure functions from the outline and each branch's own
 * entries to lanes, connectors, markers, bins and text rows. Everything is in
 * "pos" space, the rank of a tick among all entries' ticks, so the layout is
 * placed by step and never by clock time. No DOM, no pixels, no orientation.
 */
import type { HistoryOutline } from 'blaym-notebook-types';
import type { ContextEntry } from './context';

/** Text rows are at least this many pixels apart (the renderer turns it into pos with the zoom). */
export const ROW_GAP = 42;
/** The same, zoomed far out. */
export const ROW_GAP_FAR = 66;
/** A connector's curve is this long along the axis, in pixels, unless the first marker is nearer. */
export const CONNECTOR_EXTENT = 44;
/** One bin is about this many pixels long: a little more than a badge, so two badges on a lane never overlap. */
export const BIN_PX = 24;
/** Zoom levels in pos per pixel: at or below CLOSE everything shows, at or below MID charts, tables and prompts join. */
export const CLOSE = 0.15;
export const MID = 1.5;

export type Detail = 'close' | 'mid' | 'far';

/**
 * What a marker or bin stands for, most notable first. `ai_edit` is a code
 * change by the AI; `branch` is a branch's first own entry.
 */
export const PRIORITY = [
  'run_error',
  'kernel_restart',
  'outside',
  'gap',
  'branch_point',
  'branch',
  'chart',
  'ai_edit',
  'table',
  'chat_prompt',
  'run_interrupted',
  'chat_reply',
  'run_ok',
  'insert',
  'delete',
  'move',
  'type',
  'text_output',
  'snapshot',
  'kernel_start',
  'run_started',
  'other',
  'edit'
];
const RANK = new Map(PRIORITY.map((k, i) => [k, i]));
const rank = (k: string) => RANK.get(k) ?? PRIORITY.indexOf('other');

const CODE_KINDS = new Set(['edit', 'insert', 'delete', 'move', 'type']);
const RARE = new Set(PRIORITY.slice(0, 5));
const MID_KINDS = new Set([
  ...RARE,
  'chart',
  'table',
  'chat_prompt',
  'ai_edit'
]);
const NOTABLE = new Set([
  'chat_prompt',
  'chart',
  'table',
  'branch',
  'run_error',
  'kernel_restart',
  'outside',
  'gap'
]);

export interface TreeMarker {
  tick: number;
  pos: number;
  lane: number;
  branchId: string;
  entry: ContextEntry;
  /** The entry's kind, or `ai_edit` for a code change by the AI. */
  key: string;
}

/** The name shown for a branch: the root branch, which has no parent, reads "main". */
export function branchLabel(b: {
  name: string;
  parentBranchId?: string | null;
}): string {
  return b.parentBranchId ? b.name : 'main';
}

export interface TreeLane {
  branchId: string;
  name: string;
  parentBranchId?: string;
  lane: number;
  firstPos: number;
  lastPos: number;
  /** The branch point's pos on the parent's line, absent for the root. */
  parentPos?: number;
}

export interface TreeConnector {
  branchId: string;
  /** The branch whose lane holds the branch-point tick. */
  ownerId: string;
  fromLane: number;
  fromPos: number;
  toLane: number;
  toPos: number;
}

export interface TreeLayout {
  markers: TreeMarker[];
  lanes: TreeLane[];
  connectors: TreeConnector[];
  laneCount: number;
  /** Number of positions: one per distinct tick. */
  size: number;
}

export interface TreeBin {
  branchId: string;
  lane: number;
  startPos: number;
  endPos: number;
  count: number;
  kinds: Record<string, number>;
  /** The most notable kind in the bin that the zoom still shows a badge for, null when none. */
  top: string | null;
  /** The marker the badge stands for. */
  topMarker: TreeMarker | null;
  aiKnown: number;
  aiGuess: number;
  /** The bin's count over the busiest bin's, 0 to 1: the density ribbon. */
  share: number;
}

export interface TreeRow {
  marker: TreeMarker;
  key: string;
  text: string;
  /** How many nearer notable entries were folded into this row. */
  more: number;
}

function lowerBound(a: number[], v: number): number {
  let lo = 0;
  let hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (a[mid] < v) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  return lo;
}

const keyOf = (e: ContextEntry): string =>
  e.author === 'ai' && CODE_KINDS.has(e.kind) ? 'ai_edit' : e.kind;

/** Lanes, connectors and markers for every branch that has own entries. */
export function layoutTree(
  outline: Pick<HistoryOutline, 'branches'>,
  own: (branchId: string) => ContextEntry[]
): TreeLayout {
  const entries = new Map(
    outline.branches.map(b => [b.branchId, own(b.branchId)])
  );
  const all = new Set<number>();
  entries.forEach(es => es.forEach(e => all.add(e.tick)));
  const ticks = [...all].sort((a, b) => a - b);
  const posOf = (t: number) => lowerBound(ticks, t);

  const markers: TreeMarker[] = [];
  const lanes: TreeLane[] = [];
  const byId = new Map<string, TreeLane>();
  for (const b of outline.branches) {
    const es = entries.get(b.branchId) ?? [];
    if (es.length === 0) {
      continue;
    }
    const lane: TreeLane = {
      branchId: b.branchId,
      name: branchLabel(b),
      parentBranchId: b.parentBranchId,
      lane: 0,
      firstPos: posOf(es[0].tick),
      lastPos: posOf(es[es.length - 1].tick),
      // The last position at or below the branch point.
      parentPos:
        b.parentTick == null
          ? undefined
          : Math.max(0, lowerBound(ticks, b.parentTick + 1) - 1)
    };
    lanes.push(lane);
    byId.set(b.branchId, lane);
    es.forEach((entry, i) =>
      markers.push({
        tick: entry.tick,
        pos: posOf(entry.tick),
        lane: 0,
        branchId: b.branchId,
        entry,
        key: i === 0 && b.parentBranchId ? 'branch' : keyOf(entry)
      })
    );
  }
  lanes.sort((a, b) => a.firstPos - b.firstPos);
  markers.sort((a, b) => a.pos - b.pos);

  // The root keeps lane 0; a child takes the free lane nearest its parent's, and a lane is free after its branch's last entry.
  const laneEnd: number[] = [];
  for (const l of lanes) {
    const parent = l.parentBranchId ? byId.get(l.parentBranchId) : undefined;
    if (!parent) {
      l.lane = laneEnd.length === 0 ? 0 : laneEnd.length;
    } else {
      let best = -1;
      let bestScore = Infinity;
      for (let i = 0; i <= laneEnd.length; i++) {
        if (i < laneEnd.length && laneEnd[i] >= l.firstPos) {
          continue;
        }
        const score =
          Math.abs(i - parent.lane) * 2 +
          (i < parent.lane ? 0.5 : 0) +
          (i === laneEnd.length ? 0.25 : 0);
        if (score < bestScore) {
          best = i;
          bestScore = score;
        }
      }
      l.lane = best;
    }
    laneEnd[l.lane] = l.lastPos;
  }
  for (const m of markers) {
    m.lane = byId.get(m.branchId)?.lane ?? 0;
  }

  // A connector runs from the lane that owns the branch-point tick to the child's first entry.
  const connectors: TreeConnector[] = [];
  for (const l of lanes) {
    if (l.parentPos == null) {
      continue;
    }
    // The branch whose own ticks hold the branch point, else the parent itself.
    const pt = outline.branches.find(
      b => b.branchId === l.branchId
    )?.parentTick;
    const holder = outline.branches.find(b =>
      b.ownTicks.includes(pt as number)
    );
    const owner =
      (holder && byId.get(holder.branchId)) ?? byId.get(l.parentBranchId!);
    if (!owner) {
      continue;
    }
    connectors.push({
      branchId: l.branchId,
      ownerId: owner.branchId,
      fromLane: owner.lane,
      fromPos: l.parentPos,
      toLane: l.lane,
      toPos: l.firstPos
    });
  }
  return {
    markers,
    lanes,
    connectors,
    laneCount: laneEnd.length,
    size: ticks.length
  };
}

/** Where a connector's curve ends, from the branch point `a0` to the first marker `a1` (same unit, e.g. pixels): never beyond the gap. */
export function connectorEnd(
  a0: number,
  a1: number,
  extent = CONNECTOR_EXTENT
): number {
  return a0 + (a1 >= a0 ? 1 : -1) * Math.min(extent, Math.abs(a1 - a0));
}

export function detailAt(posPerPx: number): Detail {
  return posPerPx <= CLOSE ? 'close' : posPerPx <= MID ? 'mid' : 'far';
}

function shown(detail: Detail, key: string): boolean {
  return detail === 'close' || (detail === 'mid' ? MID_KINDS : RARE).has(key);
}

/** Markers grouped per branch into bins about BIN_PX long at this zoom (pos per pixel); close in, one bin per marker. */
export function binsAt(layout: TreeLayout, posPerPx: number): TreeBin[] {
  const detail = detailAt(posPerPx);
  const width = Math.max(1, Math.ceil(posPerPx * BIN_PX));
  const bins: TreeBin[] = [];
  const open = new Map<string, TreeBin>();
  const cells = new Map<TreeBin, number>();
  for (const m of layout.markers) {
    const cell = Math.floor(m.pos / width);
    let bin = open.get(m.branchId);
    if (!bin || cells.get(bin) !== cell) {
      bin = {
        branchId: m.branchId,
        lane: m.lane,
        startPos: m.pos,
        endPos: m.pos,
        count: 0,
        kinds: {},
        top: null,
        topMarker: null,
        aiKnown: 0,
        aiGuess: 0,
        share: 0
      };
      cells.set(bin, cell);
      open.set(m.branchId, bin);
      bins.push(bin);
    }
    bin.count++;
    bin.endPos = m.pos;
    bin.kinds[m.key] = (bin.kinds[m.key] ?? 0) + 1;
    if (m.entry.author === 'ai') {
      if (m.entry.confidence === 'known') {
        bin.aiKnown++;
      } else {
        bin.aiGuess++;
      }
    }
    if (shown(detail, m.key) && (!bin.top || rank(m.key) < rank(bin.top))) {
      bin.top = m.key;
      bin.topMarker = m;
    }
  }
  // Cell edges can leave two badges one position apart: fold neighbours whose badges are nearer than a bin into one.
  const folded: TreeBin[] = [];
  const last = new Map<string, TreeBin>();
  for (const b of bins) {
    const prev = last.get(b.branchId);
    const a = prev?.topMarker;
    const c = b.topMarker;
    if (prev && a && c && c.pos - a.pos < width) {
      prev.endPos = b.endPos;
      prev.count += b.count;
      prev.aiKnown += b.aiKnown;
      prev.aiGuess += b.aiGuess;
      for (const [k, n] of Object.entries(b.kinds)) {
        prev.kinds[k] = (prev.kinds[k] ?? 0) + n;
      }
      if (b.top && prev.top && rank(b.top) < rank(prev.top)) {
        prev.top = b.top;
        prev.topMarker = c;
      }
      continue;
    }
    folded.push(b);
    last.set(b.branchId, b);
  }
  const busiest = Math.max(1, ...folded.map(b => b.count));
  folded.forEach(b => (b.share = b.count / busiest));
  return folded;
}

/**
 * Text rows for notable entries only, at least ROW_GAP pixels apart at this
 * zoom; nearer notable entries fold into "more" on the most important one.
 */
export function rowsAt(
  layout: TreeLayout,
  posPerPx: number,
  text: (entry: ContextEntry, key: string, branchName: string) => string
): TreeRow[] {
  const gap = (detailAt(posPerPx) === 'far' ? ROW_GAP_FAR : ROW_GAP) * posPerPx;
  const names = new Map(layout.lanes.map(l => [l.branchId, l.name]));
  const notable = layout.markers
    .filter(m => NOTABLE.has(m.key))
    .sort((a, b) => rank(a.key) - rank(b.key) || b.pos - a.pos);
  const rows: TreeRow[] = [];
  for (const m of notable) {
    const near = rows.find(r => Math.abs(r.marker.pos - m.pos) < gap);
    if (near) {
      near.more++;
    } else {
      rows.push({
        marker: m,
        key: m.key,
        text: text(m.entry, m.key, names.get(m.branchId) ?? ''),
        more: 0
      });
    }
  }
  return rows.sort((a, b) => a.marker.pos - b.marker.pos);
}
