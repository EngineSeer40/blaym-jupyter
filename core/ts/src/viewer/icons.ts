/**
 * The one icon set: a map from timeline kind to a d3 custom symbol, drawn as a
 * flat badge with a pictogram. These are placeholders ported from
 * `probes/s6-bar/symbols.js`. Swapping the set is one import: another module
 * that exports `renderIcon` (same signature) replaces this one in the files
 * that import it (`context.ts`, `shell.ts`). Colours come only from the
 * `--bv-*` tokens (`theme.ts`).
 */
import { symbol } from 'd3';
import type { SymbolType } from 'd3';
import type {
  AuthorConfidence,
  AuthorHint,
  TimelineKind
} from 'blaym-notebook-types';

type Ctx = Parameters<SymbolType['draw']>[0];
type Point = [number, number];
/** Draws in unit coordinates (the badge is about +-1, the pictogram about +-0.6) scaled by `h`. */
type Draw = (c: Ctx, h: number) => void;

const TAU = 2 * Math.PI;
const poly = (c: Ctx, p: Point[], h: number) => {
  c.moveTo(p[0][0] * h, p[0][1] * h);
  for (let i = 1; i < p.length; i++) {
    c.lineTo(p[i][0] * h, p[i][1] * h);
  }
  c.closePath();
};
const box = (
  c: Ctx,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  h: number
) =>
  poly(
    c,
    [
      [x0, y0],
      [x1, y0],
      [x1, y1],
      [x0, y1]
    ],
    h
  );
const circ = (c: Ctx, x: number, y: number, r: number, h: number) => {
  c.moveTo((x + r) * h, y * h);
  c.arc(x * h, y * h, r * h, 0, TAU);
  c.closePath();
};
const sym = (fn: Draw): SymbolType => ({
  draw(c, size) {
    fn(c, Math.sqrt(size) / 2);
  }
});
/** A four-point sparkle with concave sides, centred at (ox, oy). */
const sparkle = (c: Ctx, h: number, k: number, ox = 0, oy = 0, w = 0.3) =>
  poly(
    c,
    [
      [ox, oy - k],
      [ox + w * k, oy - w * k],
      [ox + k, oy],
      [ox + w * k, oy + w * k],
      [ox, oy + k],
      [ox - w * k, oy + w * k],
      [ox - k, oy],
      [ox - w * k, oy - w * k]
    ],
    h
  );
const ring = (
  c: Ctx,
  h: number,
  r0: number,
  r1: number,
  a0: number,
  a1: number
) => {
  c.moveTo(Math.cos(a0) * r1 * h, Math.sin(a0) * r1 * h);
  c.arc(0, 0, r1 * h, a0, a1, false);
  c.lineTo(Math.cos(a1) * r0 * h, Math.sin(a1) * r0 * h);
  c.arc(0, 0, r0 * h, a1, a0, true);
  c.closePath();
};
const roundedSquare: Draw = (c, h) => {
  const r = 0.38 * h;
  c.moveTo(-h + r, -h);
  c.lineTo(h - r, -h);
  c.arc(h - r, -h + r, r, -Math.PI / 2, 0);
  c.lineTo(h, h - r);
  c.arc(h - r, h - r, r, 0, Math.PI / 2);
  c.lineTo(-h + r, h);
  c.arc(-h + r, h - r, r, Math.PI / 2, Math.PI);
  c.lineTo(-h, -h + r);
  c.arc(-h + r, -h + r, r, Math.PI, 1.5 * Math.PI);
  c.closePath();
};
type Family =
  | 'run'
  | 'fail'
  | 'struct'
  | 'chart'
  | 'table'
  | 'ai'
  | 'restart'
  | 'outside'
  | 'gap'
  | 'branch';

/** Family colour: a token, so each theme picks its own. */
const FAMILY_COLOUR: Record<Family, string> = {
  run: 'var(--bv-kind-run)',
  fail: 'var(--bv-danger)',
  struct: 'var(--bv-kind-struct)',
  chart: 'var(--bv-kind-chart)',
  table: 'var(--bv-kind-table)',
  ai: 'var(--bv-ai)',
  restart: 'var(--bv-kind-restart)',
  outside: 'var(--bv-kind-outside)',
  gap: 'var(--bv-muted)',
  branch: 'var(--bv-kind-branch)'
};

interface Glyph {
  family: Family;
  badge: 'circle' | 'square' | 'dot';
  /** A `dot` has none. */
  pictogram?: Draw;
}

const PICTOGRAMS = {
  chart: (c, h) => {
    box(c, -0.55, 0.02, -0.27, 0.55, h);
    box(c, -0.14, -0.22, 0.14, 0.55, h);
    box(c, 0.27, -0.55, 0.55, 0.55, h);
  },
  table: (c, h) => {
    for (const x of [-1, 1]) {
      for (const y of [-1, 1]) {
        box(
          c,
          x < 0 ? -0.55 : 0.08,
          y < 0 ? -0.5 : 0.08,
          x < 0 ? -0.08 : 0.55,
          y < 0 ? -0.08 : 0.5,
          h
        );
      }
    }
  },
  runOk: (c, h) =>
    poly(
      c,
      [
        [-0.28, -0.52],
        [0.55, 0],
        [-0.28, 0.52]
      ],
      h
    ),
  runStarted: (c, h) =>
    poly(
      c,
      [
        [-0.2, -0.38],
        [0.4, 0],
        [-0.2, 0.38]
      ],
      h
    ),
  runFailed: (c, h) =>
    poly(
      c,
      [
        [-0.55, -0.33],
        [-0.33, -0.55],
        [0, -0.22],
        [0.33, -0.55],
        [0.55, -0.33],
        [0.22, 0],
        [0.55, 0.33],
        [0.33, 0.55],
        [0, 0.22],
        [-0.33, 0.55],
        [-0.55, 0.33],
        [-0.22, 0]
      ],
      h
    ),
  runInterrupted: (c, h) => box(c, -0.4, -0.4, 0.4, 0.4, h),
  insert: (c, h) => {
    box(c, -0.14, -0.58, 0.14, 0.58, h);
    box(c, -0.58, -0.14, 0.58, 0.14, h);
  },
  delete: (c, h) => box(c, -0.58, -0.14, 0.58, 0.14, h),
  move: (c, h) => {
    poly(
      c,
      [
        [-0.6, -0.05],
        [-0.28, -0.55],
        [0.04, -0.05]
      ],
      h
    );
    poly(
      c,
      [
        [0.04, 0.05],
        [0.36, 0.55],
        [0.68, 0.05]
      ],
      h
    );
  },
  chatPrompt: (c, h) =>
    poly(
      c,
      [
        [-0.6, -0.48],
        [0.6, -0.48],
        [0.6, 0.2],
        [-0.08, 0.2],
        [-0.4, 0.58],
        [-0.36, 0.2],
        [-0.6, 0.2]
      ],
      h
    ),
  chatReply: (c, h) =>
    poly(
      c,
      [
        [-0.6, -0.48],
        [0.6, -0.48],
        [0.6, 0.2],
        [0.36, 0.2],
        [0.4, 0.58],
        [0.08, 0.2],
        [-0.6, 0.2]
      ],
      h
    ),
  kernelRestart: (c, h) => {
    const a = 5.0;
    ring(c, h, 0.36, 0.6, 0.5, a);
    poly(
      c,
      [
        [Math.cos(a) * 0.22, Math.sin(a) * 0.22],
        [Math.cos(a) * 0.74, Math.sin(a) * 0.74],
        [Math.cos(a + 0.6) * 0.48, Math.sin(a + 0.6) * 0.48]
      ],
      h
    );
  },
  outside: (c, h) => {
    box(c, -0.58, -0.5, 0, -0.34, h);
    box(c, -0.58, -0.5, -0.42, 0.5, h);
    box(c, -0.58, 0.34, 0, 0.5, h);
    box(c, -0.2, -0.08, 0.3, 0.08, h);
    poly(
      c,
      [
        [0.22, -0.32],
        [0.64, 0],
        [0.22, 0.32]
      ],
      h
    );
  },
  gap: (c, h) => {
    poly(
      c,
      [
        [-0.64, -0.1],
        [-0.1, -0.1],
        [-0.22, 0.1],
        [-0.64, 0.1]
      ],
      h
    );
    poly(
      c,
      [
        [0.1, -0.1],
        [0.64, -0.1],
        [0.64, 0.1],
        [0.22, 0.1]
      ],
      h
    );
  },
  branchPoint: (c, h) => {
    box(c, -0.08, 0.1, 0.08, 0.62, h);
    poly(
      c,
      [
        [-0.05, 0.2],
        [0.08, 0.1],
        [-0.34, -0.4],
        [-0.48, -0.3]
      ],
      h
    );
    poly(
      c,
      [
        [0.05, 0.2],
        [-0.08, 0.1],
        [0.34, -0.4],
        [0.48, -0.3]
      ],
      h
    );
    circ(c, -0.42, -0.42, 0.14, h);
    circ(c, 0.42, -0.42, 0.14, h);
  }
} satisfies Record<string, Draw>;

const P = PICTOGRAMS;
const GLYPHS: Record<TimelineKind, Glyph> = {
  edit: { family: 'struct', badge: 'dot' },
  insert: { family: 'struct', badge: 'circle', pictogram: P.insert },
  delete: { family: 'struct', badge: 'circle', pictogram: P.delete },
  move: { family: 'struct', badge: 'circle', pictogram: P.move },
  type: { family: 'struct', badge: 'dot' },
  run_started: { family: 'run', badge: 'square', pictogram: P.runStarted },
  run_ok: { family: 'run', badge: 'square', pictogram: P.runOk },
  run_error: { family: 'fail', badge: 'square', pictogram: P.runFailed },
  run_interrupted: {
    family: 'fail',
    badge: 'square',
    pictogram: P.runInterrupted
  },
  chart: { family: 'chart', badge: 'circle', pictogram: P.chart },
  table: { family: 'table', badge: 'square', pictogram: P.table },
  text_output: { family: 'table', badge: 'dot' },
  chat_prompt: { family: 'ai', badge: 'circle', pictogram: P.chatPrompt },
  chat_reply: { family: 'ai', badge: 'circle', pictogram: P.chatReply },
  kernel_start: { family: 'restart', badge: 'dot' },
  kernel_restart: {
    family: 'restart',
    badge: 'circle',
    pictogram: P.kernelRestart
  },
  outside: { family: 'outside', badge: 'square', pictogram: P.outside },
  gap: { family: 'gap', badge: 'circle', pictogram: P.gap },
  branch_point: { family: 'branch', badge: 'circle', pictogram: P.branchPoint },
  snapshot: { family: 'gap', badge: 'dot' },
  other: { family: 'gap', badge: 'dot' }
};

/** What a kind this viewer does not know looks like: a plain dot. */
const GENERIC: Glyph = GLYPHS.other;

const SVG_NS = 'http://www.w3.org/2000/svg';
const BADGE = 24;
const VIEW = 36;

/** Sets style through the style object: a `style` attribute is blocked by the page's CSP. */
function css(el: SVGElement, props: Record<string, string>): void {
  for (const [k, v] of Object.entries(props)) {
    el.style.setProperty(k, v);
  }
}

function path(
  type: SymbolType,
  d: number,
  fill: string,
  extra: Record<string, string> = {}
): SVGPathElement {
  const el = document.createElementNS(SVG_NS, 'path');
  el.setAttribute(
    'd',
    symbol()
      .type(type)
      .size(d * d)() ?? ''
  );
  css(el, { fill });
  for (const [k, v] of Object.entries(extra)) {
    el.setAttribute(k, v);
  }
  return el;
}

export function isKnownKind(kind: string): kind is TimelineKind {
  return Object.prototype.hasOwnProperty.call(GLYPHS, kind);
}

/**
 * One icon as an SVG element, `sizePx` wide and high. An AI author adds a
 * sparkle corner; a `guess` adds a dashed ring. A kind not in the set draws the
 * generic dot.
 */
export function renderIcon(
  kind: string,
  author: AuthorHint | null,
  confidence: AuthorConfidence | null,
  sizePx: number
): SVGSVGElement {
  const glyph = isKnownKind(kind) ? GLYPHS[kind] : GENERIC;
  const colour = FAMILY_COLOUR[glyph.family];
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `${-VIEW / 2} ${-VIEW / 2} ${VIEW} ${VIEW}`);
  svg.setAttribute('width', String(sizePx));
  svg.setAttribute('height', String(sizePx));
  svg.setAttribute('class', 'bv-icon');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', kind);
  if (confidence === 'guess') {
    const r = document.createElementNS(SVG_NS, 'circle');
    r.setAttribute('r', String(BADGE / 2 + 3.5));
    r.setAttribute('class', 'bv-icon-guess');
    css(r, {
      fill: 'none',
      stroke: colour,
      'stroke-width': '1.4',
      'stroke-dasharray': '3 2.2'
    });
    svg.append(r);
  }
  if (glyph.badge === 'dot') {
    svg.append(
      path(
        sym((c, h) => circ(c, 0, 0, 0.3, h)),
        BADGE,
        colour
      )
    );
  } else {
    const badge = sym(
      glyph.badge === 'circle' ? (c, h) => circ(c, 0, 0, 1, h) : roundedSquare
    );
    svg.append(path(badge, BADGE, colour));
    svg.append(
      path(sym(glyph.pictogram ?? (() => undefined)), BADGE, 'var(--bv-card)')
    );
  }
  if (author === 'ai' && glyph.family !== 'ai') {
    const k = BADGE * 0.32;
    const corner = document.createElementNS(SVG_NS, 'g');
    corner.setAttribute(
      'transform',
      `translate(${BADGE * 0.38},${-BADGE * 0.38})`
    );
    const disc = document.createElementNS(SVG_NS, 'circle');
    disc.setAttribute('r', String(k));
    css(disc, {
      fill: 'var(--bv-ai)',
      stroke: 'var(--bv-card)',
      'stroke-width': '1.5'
    });
    corner.append(disc);
    corner.append(
      path(
        sym((c, h) => sparkle(c, h, 0.66, 0, 0, 0.24)),
        k * 2.1,
        'var(--bv-card)'
      )
    );
    svg.append(corner);
  }
  return svg;
}
