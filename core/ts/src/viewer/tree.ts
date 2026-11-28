/**
 * The tree view of the side panel: branch lanes with event badges, newest at
 * the top, drawn as SVG. Only the visible section plus a margin is in the
 * page. A click or a drag on a lane, badge or text row moves through
 * `focus.goto` (one request per animation frame) and selects the entry; hover
 * shows a tooltip. Reads the layout and the focus state; changes nothing but
 * through the session. Vertical only in this version.
 */
import { curveBumpY, line } from 'd3';
import type { ContextEntry } from './context';
import { LABELS } from './context';
import { cellName } from './cellname';
import type { CellNamer } from './cellname';
import type { IFocusItem } from './focus';
import { renderIcon } from './icons';
import { createOverview } from './overview';
import type { Session } from './session';
import type { Timelines } from './timelines';
import {
  binsAt,
  branchLabel,
  connectorEnd,
  layoutTree,
  rowsAt
} from './treelayout';
import type { TreeBin, TreeLayout, TreeMarker, TreeRow } from './treelayout';
import {
  alongOf,
  centerOff,
  clampOff,
  contentLength,
  edgeSpeed,
  fitStep,
  followOff,
  offForAnchor,
  offScreen,
  posOf,
  zoomStep
} from './treeview';
import type { Viewport } from './treeview';
import { formatTime } from './versions';
import type { Frames } from './frames';

const SVG_NS = 'http://www.w3.org/2000/svg';
/** The zoom the tree opens at, in pixels per position. */
const START_STEP = 12;
/** How long a manual scroll pauses auto-follow, in milliseconds. */
const MANUAL_PAUSE = 800;
/** The far-jump animation, in milliseconds. */
const ANIMATE_MS = 250;
/** A far jump is animated only when it is longer than this, in pixels. */
const FAR = 40;
/** One zoom button press, as a factor. */
const ZOOM_BUTTON = 1.5;
/** Play steps this many ticks a second. */
const PLAY_RATE = 10;
/** A requested step counts as in flight for this long, in milliseconds. */
const IN_FLIGHT = 600;
const PAD = 20;
const LANE_ORIGIN = 26;
const LANE_GAP = 32;
/** The icon's box; its badge is two thirds of it, about 22 px. */
const ICON = 33;
const ROW_ICON = 21;
/** A plain dot is drawn only with this much room to both neighbours on its lane, in pixels. */
const DOT_ROOM = 22;
/** The section drawn beyond the visible part, in pixels. */
const MARGIN = 240;
/** How near a pointer must be to a badge or a lane, in pixels. */
const REACH = 12;
const CHAR_PX = 6.6;
/** Steps that are only a small dot (with a tooltip); an AI code change is always a badge, key `ai_edit`. */
const PLAIN = new Set([
  'edit',
  'run_ok',
  'run_started',
  'chat_reply',
  'move',
  'delete',
  'type',
  'text_output',
  'snapshot',
  'kernel_start',
  'other'
]);
const EMPTY_WIDTH = 280;

export interface TreeOptions {
  /** Opening zoom in pixels per position, and the opening scroll: `oldest`, or an offset in pixels (default: the newest end). For the harness. */
  zoom?: number;
  scroll?: 'oldest' | number;
  /** Auto-follow, as remembered, and where to remember a change. */
  follow?: boolean;
  onFollowChange?(on: boolean): void;
  /** Told when the playhead is moving (a drag, Play, key steps) and when the pointer is released. */
  frames?: Frames;
}

export interface TreeView {
  /** Reads the branches and entries again, then draws. */
  update(): void;
  /** Draws again, for when the tree has just become visible; keeps the scroll position. */
  refresh(): void;
}

function node<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number>,
  cls?: string
): SVGElementTagNameMap[K] {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    e.setAttribute(k, String(v));
  }
  if (cls) {
    e.setAttribute('class', cls);
  }
  return e;
}

const itemOf = (m: TreeMarker): IFocusItem => ({
  firstTick: m.tick,
  lastTick: m.tick,
  kind: m.entry.kind
});

function nearestByPos(list: TreeMarker[], pos: number): TreeMarker | null {
  let best: TreeMarker | null = null;
  for (const m of list) {
    if (!best || Math.abs(m.pos - pos) < Math.abs(best.pos - pos)) {
      best = m;
    }
  }
  return best;
}

export function createTree(
  target: HTMLElement,
  session: Session,
  timelines: Pick<Timelines, 'branches' | 'own' | 'viewed'>,
  nameCell: CellNamer = (_id, index) => cellName(undefined, index),
  options: TreeOptions = {}
): TreeView {
  let layout: TreeLayout = {
    markers: [],
    lanes: [],
    connectors: [],
    laneCount: 0,
    size: 0
  };
  let bins: TreeBin[] = [];
  let byBranch = new Map<string, TreeMarker[]>();
  let roomy = new Set<TreeMarker>();
  let rows: TreeRow[] = [];
  let rowY = new Map<TreeRow, number>();
  let picked: string | undefined;
  let scrub: { branchId: string; marker: TreeMarker } | null = null;
  let scrubbing = false;
  let raf = 0;
  let dirty = false;
  let sent: number | undefined;
  let hoveredTick: number | undefined;
  // Scroll and zoom: `off` is the window's start along the content.
  let step = options.zoom ?? START_STEP;
  let off = 0;
  let opened = false;
  let anim: { from: number; to: number; t0: number } | null = null;
  let follow = options.follow !== false;
  let manualUntil = 0;
  let lastClientY: number | undefined;
  let playing = false;
  let lastStep = 0;
  let requested: { tick: number; at: number } | undefined;

  target.replaceChildren();
  const root = document.createElement('div');
  root.className = 'bv-tree-root';
  root.tabIndex = 0;
  const bar = document.createElement('div');
  bar.className = 'bv-tree-bar';
  const button = (text: string, label: string) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'bv-tree-btn';
    b.textContent = text;
    b.title = label;
    b.setAttribute('aria-label', label);
    return b;
  };
  const zoomOut = button('\u2212', 'Zoom out');
  const zoomIn = button('+', 'Zoom in');
  const fit = button('fit', 'Fit the whole history');
  const playBtn = button('Play', 'Play forward along this branch');
  const followLabel = document.createElement('label');
  followLabel.className = 'bv-tree-follow';
  const followBox = document.createElement('input');
  followBox.type = 'checkbox';
  followBox.checked = follow;
  followLabel.append(followBox, ' follow');
  followLabel.title = 'Scroll to keep the playhead in view';
  // The selected branch's name, with "switch to this branch" when it is not the one being recorded.
  const branchName = document.createElement('span');
  branchName.className = 'bv-tree-branch';
  const switchBtn = button(
    'switch to this branch',
    "Switch the notebook to this branch's latest state"
  );
  switchBtn.classList.add('bv-tree-switch');
  switchBtn.addEventListener('click', () => session.switchTo(selectedBranch()));
  bar.append(zoomOut, zoomIn, fit, followLabel, playBtn, branchName, switchBtn);
  const body = document.createElement('div');
  body.className = 'bv-tree-body';
  const win = document.createElement('div');
  win.className = 'bv-tree-win';
  const ovHost = document.createElement('div');
  ovHost.className = 'bv-ov';
  const overview = createOverview(ovHost);
  const offBtn = button('', 'Scroll back to the playhead');
  offBtn.classList.add('bv-tree-off');
  offBtn.hidden = true;
  const scroll = document.createElement('div');
  scroll.className = 'bv-tree-scroll';
  const inner = document.createElement('div');
  inner.className = 'bv-tree-inner';
  const svg = node('svg', { width: '100%' }, 'bv-tree-svg');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'branch tree');
  const tip = document.createElement('div');
  tip.className = 'bv-tree-tip';
  tip.hidden = true;
  const empty = document.createElement('p');
  empty.className = 'bv-muted bv-version-empty';
  empty.textContent = 'No history yet.';
  inner.append(svg, tip);
  scroll.append(inner);
  win.append(scroll, offBtn);
  body.append(win, ovHost);
  root.append(bar, body);
  target.append(root);

  /** The window over the content, for the mapping. */
  const vp = (): Viewport => ({
    size: layout.size,
    step,
    length: scroll.clientHeight || 600,
    pad: PAD,
    up: true
  });
  const now = () => window.performance.now();

  const geo = () => ({
    labelX: LANE_ORIGIN + Math.max(0, layout.laneCount - 1) * LANE_GAP + 30,
    laneX: (lane: number) => LANE_ORIGIN + lane * LANE_GAP,
    width: scroll.clientWidth || EMPTY_WIDTH
  });

  const rowText = (entry: ContextEntry, key: string, branch: string) => {
    if (key === 'branch') {
      return branch;
    }
    const label = LABELS[entry.kind] ?? entry.kind;
    return entry.cellId && /^(chart|table|run_error)$/.test(entry.kind)
      ? `${label} · ${nameCell(entry.cellId, entry.cellIndex)}`
      : label;
  };

  const selectedBranch = () => picked ?? session.view().branchId;

  /** The marker at or before `tick` on the branch's line, for the playhead. */
  function playheadMarker(): TreeMarker | null {
    const view = session.view();
    if (view.tick === undefined) {
      return null;
    }
    let best: TreeMarker | null = null;
    for (const m of layout.markers) {
      if (m.tick <= view.tick && (!best || m.tick >= best.tick)) {
        best = m;
      }
    }
    return best;
  }

  function draw(): void {
    if (layout.size === 0) {
      target.replaceChildren(empty);
      return;
    }
    if (empty.parentElement) {
      target.replaceChildren(root);
    }
    const v = vp();
    const s = {
      px: (pos: number) => alongOf(v, pos),
      height: contentLength(v)
    };
    const g = geo();
    const viewTop = off;
    const viewBottom = viewTop + v.length;
    const lo = viewTop - MARGIN;
    const hi = viewBottom + MARGIN;
    const sel = selectedBranch();
    const meta = timelines.branches().find(b => b.branchId === sel);
    branchName.textContent = meta ? branchLabel(meta) : '';
    switchBtn.hidden = !meta || sel === session.activeBranchId();
    switchBtn.disabled = session.switching();
    inner.style.setProperty('height', `${s.height}px`);
    svg.setAttribute('height', String(s.height));
    scroll.scrollTop = off;
    const out: SVGElement[] = [];
    const curve = line().curve(curveBumpY);

    // Lanes and connectors: every one grey, the selected branch last and in the highlight.
    const lanePaths: [boolean, SVGElement][] = [];
    const ends = new Map<string, number>();
    for (const c of layout.connectors) {
      const y0 = s.px(c.fromPos);
      const yEnd = connectorEnd(y0, s.px(c.toPos));
      ends.set(c.branchId, yEnd);
      if (Math.max(y0, yEnd) < lo || Math.min(y0, yEnd) > hi) {
        continue;
      }
      const d = curve([
        [g.laneX(c.fromLane), y0],
        [g.laneX(c.toLane), yEnd]
      ]);
      lanePaths.push([
        c.branchId === sel,
        node('path', { d: d ?? '' }, 'bv-tree-lane')
      ]);
    }
    for (const l of layout.lanes) {
      const yBottom = ends.get(l.branchId) ?? s.px(l.firstPos);
      const yTop = s.px(l.lastPos);
      if (yBottom < lo || yTop > hi) {
        continue;
      }
      const laneLine = node(
        'line',
        {
          x1: g.laneX(l.lane),
          x2: g.laneX(l.lane),
          y1: Math.min(hi, yBottom),
          y2: Math.max(lo, yTop)
        },
        'bv-tree-lane'
      );
      laneLine.setAttribute('data-branch', l.branchId);
      lanePaths.push([l.branchId === sel, laneLine]);
    }
    for (const [isSel, p] of lanePaths.sort((a, b) => +a[0] - +b[0])) {
      if (isSel) {
        p.classList.add('bv-tree-lane-sel');
      }
      out.push(p);
    }

    // Badges and dots.
    const selected = session.focus.get().selected;
    for (const b of bins) {
      const m = b.topMarker;
      if (!m) {
        continue;
      }
      const y = s.px(m.pos);
      if (y < lo || y > hi) {
        continue;
      }
      const x = g.laneX(m.lane);
      if (selected && selected.firstTick === m.tick) {
        out.push(node('circle', { cx: x, cy: y, r: 14 }, 'bv-tree-ring'));
      }
      if (PLAIN.has(m.key)) {
        if (roomy.has(m)) {
          out.push(node('circle', { cx: x, cy: y, r: 3.5 }, 'bv-tree-dot'));
        }
        continue;
      }
      const icon = renderIcon(
        m.key === 'branch' ? 'branch_point' : m.entry.kind,
        m.entry.author,
        m.entry.confidence,
        ICON
      );
      icon.setAttribute('x', String(x - ICON / 2));
      icon.setAttribute('y', String(y - ICON / 2));
      icon.classList.add('bv-tree-badge');
      icon.setAttribute('data-tick', String(m.tick));
      out.push(icon);
    }

    // Branches whose first marker is below the window but whose lane is in it keep their names in a strip at the bottom edge.
    const sticky = layout.lanes.filter(
      l =>
        l.parentBranchId &&
        s.px(l.firstPos) > viewBottom &&
        s.px(l.lastPos) < viewBottom
    );
    const stripTop = viewBottom - (sticky.length ? sticky.length * 16 + 8 : 0);

    // Text rows: a small kind icon, the text (the branch name in its own style), "+N" for what was folded in.
    const textX = g.labelX + ROW_ICON - 2;
    const maxChars = Math.max(6, Math.floor((g.width - textX - 10) / CHAR_PX));
    rowY = new Map();
    for (const r of rows) {
      const y = s.px(r.marker.pos);
      if (y < lo || y > hi) {
        continue;
      }
      if (y > stripTop - 8) {
        continue;
      }
      rowY.set(r, y);
      const isSel = r.marker.branchId === sel;
      const icon = renderIcon(
        r.key === 'branch' ? 'branch_point' : r.marker.entry.kind,
        r.marker.entry.author,
        r.marker.entry.confidence,
        ROW_ICON
      );
      icon.setAttribute('x', String(g.labelX - 4));
      icon.setAttribute('y', String(y - ROW_ICON / 2));
      out.push(icon);
      const text = node(
        'text',
        { x: textX, y },
        `bv-tree-row${r.key === 'branch' ? ' bv-tree-branch' : ''}${isSel ? ' bv-tree-row-sel' : ''}`
      );
      text.setAttribute('data-tick', String(r.marker.tick));
      text.setAttribute('data-branch', r.marker.branchId);
      const room = r.more ? maxChars - 5 : maxChars;
      text.textContent =
        r.text.length > room ? `${r.text.slice(0, room - 1)}…` : r.text;
      if (r.more) {
        const more = node('tspan', {}, 'bv-tree-more');
        more.textContent = `  +${r.more}`;
        text.append(more);
      }
      out.push(text);
    }
    if (sticky.length) {
      out.push(
        node(
          'rect',
          {
            x: g.labelX - 8,
            y: stripTop,
            width: Math.max(0, g.width - g.labelX + 8),
            height: viewBottom - stripTop
          },
          'bv-tree-strip'
        )
      );
      sticky.forEach((l, i) => {
        const name = node(
          'text',
          { x: g.labelX, y: stripTop + 12 + i * 16 },
          `bv-tree-row bv-tree-sticky${l.branchId === sel ? ' bv-tree-row-sel' : ''}`
        );
        name.setAttribute('data-branch', l.branchId);
        name.textContent = l.name;
        out.push(name);
      });
    }

    // The playhead: a line across and a handle on the viewed branch's lane.
    const head = scrub?.marker ?? playheadMarker();
    overview.draw({
      layout,
      selectedBranchId: sel,
      headPos: head?.pos,
      window: [posOf(v, off), posOf(v, off + v.length)]
    });
    const side = head ? offScreen(v, off, head.pos) : null;
    offBtn.hidden = !side;
    if (side) {
      offBtn.textContent =
        side === 'start' ? 'playhead is above' : 'playhead is below';
      offBtn.classList.toggle('bv-tree-off-end', side === 'end');
    }
    if (head) {
      const y = s.px(head.pos);
      out.push(
        node('line', { x1: 0, x2: g.width, y1: y, y2: y }, 'bv-tree-head'),
        node(
          'circle',
          { cx: g.laneX(head.lane), cy: y, r: 7 },
          'bv-tree-handle'
        )
      );
    }
    svg.replaceChildren(...out);
  }

  /** The bins, rows and room for dots at the current zoom. */
  function relayout(): void {
    const v = vp();
    bins = binsAt(layout, 1 / step);
    rows = rowsAt(layout, 1 / step, rowText);
    roomy = new Set();
    const px = (pos: number) => alongOf(v, pos);
    for (const list of byBranch.values()) {
      list.forEach((m, i) => {
        const before = i > 0 ? px(list[i - 1].pos) - px(m.pos) : Infinity;
        const after =
          i < list.length - 1 ? px(m.pos) - px(list[i + 1].pos) : Infinity;
        if (Math.min(before, after) >= DOT_ROOM) {
          roomy.add(m);
        }
      });
    }
  }

  function update(): void {
    layout = layoutTree({ branches: timelines.branches() }, id =>
      timelines.own(id)
    );
    byBranch = new Map();
    for (const m of layout.markers) {
      byBranch.set(m.branchId, [...(byBranch.get(m.branchId) ?? []), m]);
    }
    if (!opened && layout.size > 0) {
      opened = true;
      const v = vp();
      step = Math.max(fitStep(v.size, v.length, PAD), step);
      off = clampOff(
        { ...v, step },
        options.scroll === 'oldest' ? 1e9 : (options.scroll ?? 0)
      );
    }
    relayout();
    draw();
  }

  // Pointer: find what is under it, then scrub, select or show a tooltip.
  const at = (ev: PointerEvent) => {
    const r = inner.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  };

  /** The branch and marker under a point: a text row, or a lane (with its nearest badge). */
  function hit(
    x: number,
    y: number
  ): { branchId: string; marker: TreeMarker; near: boolean } | null {
    const g = geo();
    if (x >= g.labelX - 6) {
      for (const [r, ry] of rowY) {
        if (Math.abs(ry - y) <= 12) {
          return { branchId: r.marker.branchId, marker: r.marker, near: false };
        }
      }
      return null;
    }
    const pos = posOf(vp(), y);
    let best: { branchId: string; d: number } | null = null;
    for (const l of layout.lanes) {
      if (Math.abs(g.laneX(l.lane) - x) > REACH) {
        continue;
      }
      const d = Math.max(l.firstPos - pos, pos - l.lastPos, 0);
      if (!best || d < best.d) {
        best = { branchId: l.branchId, d };
      }
    }
    const list = best ? byBranch.get(best.branchId) : undefined;
    const marker = list ? nearestByPos(list, pos) : null;
    return best && marker
      ? {
          branchId: best.branchId,
          marker,
          near: Math.abs(alongOf(vp(), marker.pos) - y) <= REACH
        }
      : null;
  }

  /** Asks for a frame of the loop. */
  function kick(): void {
    if (!raf) {
      raf = window.requestAnimationFrame(loop);
    }
  }

  /** Draws in the next frame. */
  function invalidate(): void {
    dirty = true;
    kick();
  }

  const lineTicks = () => timelines.viewed().map(e => e.tick);

  /** The tick one step from `base` along the viewed line (`dir` 1 is newer); undefined past the ends. */
  function nextTick(base: number | undefined, dir: 1 | -1): number | undefined {
    const ticks = lineTicks();
    return dir > 0
      ? ticks.find(t => base !== undefined && t > base)
      : [...ticks].reverse().find(t => base === undefined || t < base);
  }

  /** Moves the viewed branch's playhead along its line. Returns false at the end. */
  function stepTick(dir: 1 | -1): boolean {
    const view = session.view();
    const base =
      requested && now() - requested.at < IN_FLIGHT
        ? requested.tick
        : view.tick;
    const tick = nextTick(base, dir);
    if (tick === undefined) {
      return false;
    }
    requested = { tick, at: now() };
    manualUntil = 0;
    options.frames?.moving();
    session.focus.goto(view.branchId, tick);
    return true;
  }

  function animateTo(to: number): void {
    const clamped = clampOff(vp(), to);
    if (Math.abs(clamped - off) > 1) {
      anim = { from: off, to: clamped, t0: now() };
      kick();
    } else {
      anim = null;
      off = clamped;
      invalidate();
    }
  }

  function scrollNow(to: number): void {
    anim = null;
    off = clampOff(vp(), to);
    invalidate();
  }

  /** Auto-follow: keep the playhead inside the inner margin; off the window altogether, animate back. */
  function followHead(): void {
    const head = playheadMarker();
    if (!follow || !head || scrubbing || now() < manualUntil) {
      return;
    }
    const r = followOff(vp(), off, head.pos);
    if (r.jump) {
      animateTo(r.off);
    } else if (r.off !== off) {
      scrollNow(r.off);
    }
  }

  function zoomAt(factor: number, anchor: number): void {
    const v = vp();
    const pos = posOf(v, off + anchor);
    step = zoomStep(v, factor);
    anim = null;
    off = offForAnchor(vp(), pos, anchor);
    relayout();
    invalidate();
  }

  /** The one loop: scrub requests, animation, edge scrolling, Play and drawing. */
  function loop(t: number): void {
    raf = 0;
    if (scrub && scrub.marker.tick !== sent) {
      sent = scrub.marker.tick;
      session.focus.goto(scrub.branchId, scrub.marker.tick);
    }
    if (anim) {
      const k = Math.min(1, (now() - anim.t0) / ANIMATE_MS);
      off = anim.from + (anim.to - anim.from) * (1 - Math.pow(1 - k, 3));
      dirty = true;
      if (k >= 1) {
        anim = null;
      }
    }
    if (scrubbing && scrub && lastClientY !== undefined) {
      const v = vp();
      const a = lastClientY - scroll.getBoundingClientRect().top;
      const sp = edgeSpeed(v.length, a);
      const to = clampOff(v, off + sp * 24);
      if (sp && to !== off) {
        anim = null;
        off = to;
        dirty = true;
        scrubTo(scrub.branchId, off + a);
      }
    }
    if (playing && t - lastStep >= 1000 / PLAY_RATE) {
      lastStep = t;
      if (!stepTick(1)) {
        stopPlay();
        session.backToNow();
      }
    }
    if (dirty) {
      dirty = false;
      draw();
    }
    if (anim || scrubbing || playing) {
      kick();
    }
  }

  function stopPlay(): void {
    playing = false;
    playBtn.textContent = 'Play';
    playBtn.setAttribute('aria-pressed', 'false');
  }

  function showTip(m: TreeMarker | null): void {
    if (!m) {
      tip.hidden = true;
      return;
    }
    const e = m.entry;
    const who =
      e.author === 'person'
        ? 'user'
        : e.author === 'unknown'
          ? ''
          : e.author === 'ai'
            ? 'AI'
            : e.author;
    tip.textContent = [
      LABELS[e.kind] ?? e.kind,
      formatTime(e.timeMs),
      who && (e.confidence === 'guess' ? `${who} · guess` : who)
    ]
      .filter(Boolean)
      .join(' · ');
    tip.hidden = false;
    tip.style.setProperty('left', `${geo().labelX}px`);
    tip.style.setProperty('top', `${alongOf(vp(), m.pos) + 14}px`);
  }

  function hover(m: TreeMarker | null): void {
    showTip(m);
    if (m?.tick !== hoveredTick) {
      hoveredTick = m?.tick;
      session.focus.hover(m ? itemOf(m) : null);
    }
  }

  function scrubTo(branchId: string, y: number): void {
    const list = byBranch.get(branchId);
    const marker = list ? nearestByPos(list, posOf(vp(), y)) : null;
    if (marker && marker.tick !== scrub?.marker.tick) {
      if (scrub) {
        // Not the first press: the playhead is being dragged.
        options.frames?.moving();
      }
      scrub = { branchId, marker };
      picked = branchId;
      invalidate();
    }
  }

  inner.addEventListener('pointerdown', ev => {
    const p = at(ev);
    root.focus({ preventScroll: true });
    const h = layout.size ? hit(p.x, p.y) : null;
    if (!h) {
      return;
    }
    scrubbing = true;
    lastClientY = ev.clientY;
    kick();
    sent = undefined;
    scrub = null;
    inner.setPointerCapture(ev.pointerId);
    hover(null);
    scrubTo(h.branchId, alongOf(vp(), h.marker.pos));
  });
  inner.addEventListener('pointermove', ev => {
    const p = at(ev);
    lastClientY = ev.clientY;
    if (scrubbing && scrub) {
      scrubTo(scrub.branchId, p.y);
      return;
    }
    const h = layout.size ? hit(p.x, p.y) : null;
    hover(h && h.near ? h.marker : null);
  });
  const release = () => {
    if (!scrubbing) {
      return;
    }
    scrubbing = false;
    options.frames?.release();
    if (scrub) {
      const done = scrub;
      scrub = null;
      session.focus.select(itemOf(done.marker));
      if (done.marker.tick !== sent) {
        session.focus.goto(done.branchId, done.marker.tick);
      }
    }
    invalidate();
  };
  inner.addEventListener('pointerup', release);
  inner.addEventListener('pointercancel', release);
  inner.addEventListener('pointerleave', () => hover(null));

  // Wheel scrolls; ctrl+wheel (also a trackpad pinch) zooms around the pointer.
  scroll.addEventListener(
    'wheel',
    ev => {
      ev.preventDefault();
      const unit = ev.deltaMode === 1 ? 16 : 1;
      if (ev.ctrlKey) {
        zoomAt(
          Math.exp(-ev.deltaY * unit * 0.01),
          ev.clientY - scroll.getBoundingClientRect().top
        );
        return;
      }
      manualUntil = now() + MANUAL_PAUSE;
      scrollNow(off + ev.deltaY * unit);
    },
    { passive: false }
  );
  const zoomFromMiddle = (factor: number) => zoomAt(factor, vp().length / 2);
  zoomIn.addEventListener('click', () => zoomFromMiddle(ZOOM_BUTTON));
  zoomOut.addEventListener('click', () => zoomFromMiddle(1 / ZOOM_BUTTON));
  fit.addEventListener('click', () => {
    const v = vp();
    step = fitStep(v.size, v.length, PAD);
    anim = null;
    off = 0;
    relayout();
    invalidate();
  });
  followBox.addEventListener('change', () => {
    follow = followBox.checked;
    options.onFollowChange?.(follow);
    followHead();
  });
  offBtn.addEventListener('click', () => {
    const head = playheadMarker();
    manualUntil = 0;
    if (head) {
      animateTo(centerOff(vp(), head.pos));
    }
  });
  playBtn.addEventListener('click', () => {
    if (playing) {
      stopPlay();
      return;
    }
    playing = true;
    lastStep = 0;
    manualUntil = 0;
    playBtn.textContent = 'Pause';
    playBtn.setAttribute('aria-pressed', 'true');
    kick();
  });
  overview.onPick((pos, animate) => {
    manualUntil = now() + MANUAL_PAUSE;
    const to = centerOff(vp(), pos);
    if (animate && Math.abs(to - off) > FAR) {
      animateTo(to);
    } else {
      scrollNow(to);
    }
  });

  // Keys while the tree has focus: Up and Down one tick on the viewed line, Home and End its ends, Escape back to now.
  root.addEventListener('keydown', ev => {
    const view = session.view();
    const ticks = lineTicks();
    if (ev.key === 'ArrowUp' || ev.key === 'ArrowDown') {
      ev.preventDefault();
      stepTick(ev.key === 'ArrowUp' ? 1 : -1);
    } else if (ev.key === 'Home' || ev.key === 'End') {
      ev.preventDefault();
      const tick = ev.key === 'Home' ? ticks[ticks.length - 1] : ticks[0];
      manualUntil = 0;
      if (tick !== undefined) {
        requested = { tick, at: now() };
        session.focus.goto(view.branchId, tick);
      }
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      stopPlay();
      manualUntil = 0;
      session.backToNow();
    }
  });

  const onMoved = () => {
    if (requested && session.view().tick === requested.tick) {
      requested = undefined;
    }
    followHead();
    invalidate();
  };
  session.focus.subscribe(invalidate);
  session.subscribe(onMoved);

  return {
    update,
    refresh() {
      draw();
    }
  };
}
