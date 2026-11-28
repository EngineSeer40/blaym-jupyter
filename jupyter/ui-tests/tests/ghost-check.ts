// Helpers for the ghost book check of end-to-end.spec.ts: what JupyterLab shows at a
// checkpoint (read from the notebook model) against what the history viewer's ghost
// book shows at that tick. Fingerprints are short strings, so a mismatch reads well.

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

export interface OutFp {
  type: string;
  mimes: string[];
  /** "img:<width>x<height>", "table:<cell texts>", or "text:<start>" */
  fp: string;
}
export interface CellFp {
  index: number;
  id: string;
  type: string;
  code: string;
  count: number | null;
  outputs: OutFp[];
}
export interface Checkpoint {
  name: string;
  tick: number;
  cells: CellFp[];
}

const text = (v: unknown): string =>
  Array.isArray(v) ? v.join('') : typeof v === 'string' ? v : '';
const head = (s: string): string => s.replace(/\s+/g, ' ').trim().slice(0, 40);

/** The size an `<img>` reports for an SVG: the `width` and `height` of its root tag. */
export function svgSize(svg: string): string {
  const root = /<svg\b[^>]*>/i.exec(svg)?.[0] ?? '';
  const num = (k: string) =>
    new RegExp(`\\b${k}=["']?([\\d.]+)`).exec(root)?.[1] ?? '?';
  return `${num('width')}x${num('height')}`;
}
export function tableTexts(html: string): string {
  return [...html.matchAll(/<t[dh][^>]*>([^<]*)</g)]
    .map(m => m[1].trim())
    .join('|');
}
/** Width and height of a PNG from its header (what `<img>` reports as its natural size). */
export function pngSize(b64: string): string {
  const buf = Buffer.from(b64.replace(/\s/g, ''), 'base64');
  return `${buf.readUInt32BE(16)}x${buf.readUInt32BE(20)}`;
}

/** The fingerprint of one nbformat output, in the order the viewer draws a bundle. */
export function outputFp(o: Any): OutFp {
  const type = String(o.output_type);
  if (type === 'stream') {
    return { type, mimes: [], fp: 'text:' + head(text(o.text)) };
  }
  if (type === 'error') {
    return { type, mimes: [], fp: 'text:' + String(o.ename) };
  }
  const data = o.data ?? {};
  const mimes = Object.keys(data).sort();
  if (data['image/png'] !== undefined) {
    return { type, mimes, fp: `img:${pngSize(text(data['image/png']))}` };
  }
  if (data['image/svg+xml'] !== undefined) {
    return {
      type,
      mimes,
      fp: `img:${svgSize(text(data['image/svg+xml']))}`
    };
  }
  const plotly = data['application/vnd.plotly.v1+json'];
  if (plotly !== undefined) {
    // the ghost book draws a placeholder with the figure's title
    const title = plotly?.layout?.title;
    return {
      type,
      mimes,
      fp:
        'placeholder:application/vnd.plotly.v1+json' +
        (typeof title === 'string' ? title : (title?.text ?? ''))
    };
  }
  const html = text(data['text/html']);
  if (/<table/i.test(html)) {
    return { type, mimes, fp: 'table:' + tableTexts(html) };
  }
  return { type, mimes, fp: 'text:' + head(text(data['text/plain'])) };
}

/** The cells of the open notebook, from the model. */
export function readModel(page: Any): Promise<CellFp[]> {
  return page
    .evaluate(() => {
      const w = (window as Any).jupyterapp.shell.currentWidget;
      return w.content.widgets.map((c: Any, i: number) => {
        const isCode = c.model.type === 'code';
        return {
          index: i,
          id: String(c.model.id),
          type: String(c.model.type),
          code: String(c.model.sharedModel.getSource()),
          count: isCode ? (c.model.executionCount ?? null) : null,
          raw: isCode ? c.model.outputs.toJSON() : []
        };
      });
    })
    .then((cells: Any[]) =>
      cells.map(c => ({
        index: c.index,
        id: c.id,
        type: c.type,
        code: c.code,
        count: c.count,
        outputs: c.raw.map(outputFp)
      }))
    );
}

/** The cells of an nbformat notebook (the file read through the contents API). */
export function fileCells(content: Any): CellFp[] {
  return (content?.cells ?? []).map((c: Any, i: number) => ({
    index: i,
    id: String(c.id ?? ''),
    type: String(c.cell_type),
    code: text(c.source),
    count: c.cell_type === 'code' ? (c.execution_count ?? null) : null,
    outputs: (c.outputs ?? []).map(outputFp)
  }));
}

/** What the ghost book shows: one entry per cell card, in order, deleted-cell rows left out. */
export function readGhost(page: Any): Promise<CellFp[]> {
  return page.evaluate(async () => {
    const out: Any[] = [];
    const cards = [
      ...document.querySelectorAll('.bv-gcell:not(.bv-mark-ghost)')
    ];
    for (const [i, card] of cards.entries()) {
      // outputs fetch their blobs when they scroll into view: bring each in, then wait
      // until its content stops changing before reading it
      for (const m of card.querySelectorAll('.bv-out-item')) {
        m.scrollIntoView({ block: 'nearest' });
        let last = '';
        let still = 0;
        for (let k = 0; k < 40 && still < 3; k++) {
          await new Promise(r => setTimeout(r, 100));
          const now =
            m.innerHTML.length + ':' + m.querySelectorAll('img').length;
          still = now === last ? still + 1 : 0;
          last = now;
        }
      }
      const isText = card.classList.contains('bv-gcell-text');
      const label = card.querySelector('.bv-gcell-label')?.textContent ?? '';
      const n = /In \[(\d+)\]/.exec(label);
      const outputs: Any[] = [];
      for (const m of card.querySelectorAll('.bv-out-item')) {
        const type = m.getAttribute('data-output-type') ?? '';
        const img = m.querySelector('img');
        const table = m.querySelector('table');
        const pre = m.querySelector('pre');
        let fp = 'placeholder:' + (m.textContent ?? '').slice(0, 60);
        if (img) {
          // the viewer's CSP does not let the page fetch its own blob: URLs, so the size the
          // browser decoded from the blob stands for the content
          await img.decode().catch(() => undefined);
          fp = img.naturalWidth
            ? `img:${img.naturalWidth}x${img.naturalHeight}`
            : 'img:not decoded';
        } else if (table) {
          fp =
            'table:' +
            [...table.querySelectorAll('td,th')]
              .map(c => (c.textContent ?? '').trim())
              .join('|');
        } else if (pre) {
          fp = 'text:' + (pre.textContent ?? '').replace(/\s+/g, ' ').trim();
        }
        outputs.push({ type, mimes: [], fp });
      }
      out.push({
        index: i,
        id: card.getAttribute('data-cell-id') ?? '',
        type: isText ? 'text' : 'code',
        code: card.querySelector('.bv-gcell-code')?.textContent ?? '',
        count: n ? Number(n[1]) : null,
        outputs
      });
    }
    return out;
  });
}

const flat = (s: string) => s.replace(/\s+/g, '');

/**
 * Differences, one line each: step, cell, field, expected, got.
 * Code is compared without whitespace (the viewer's marked lines lose their line breaks
 * in `textContent`); markdown cells show as 'text' in the ghost book.
 */
export function compare(step: string, want: CellFp[], got: CellFp[]): string[] {
  const bad: string[] = [];
  const say = (i: number | string, field: string, w: unknown, g: unknown) =>
    bad.push(
      `[${step}] cell ${i}: ${field}: expected ${JSON.stringify(w)}, got ${JSON.stringify(g)}`
    );
  if (want.length !== got.length) {
    say('*', 'cell count', want.length, got.length);
  }
  for (let i = 0; i < Math.min(want.length, got.length); i++) {
    const w = want[i];
    const g = got[i];
    const at = `${i} (${w.id})`;
    const wantType = w.type === 'code' ? 'code' : 'text';
    if (wantType !== g.type) {
      say(at, 'type', wantType, g.type);
    }
    if (flat(w.code) !== flat(g.code)) {
      say(at, 'code', w.code, g.code);
    }
    if (w.type === 'code' && w.count !== g.count) {
      say(at, 'execution count', w.count, g.count);
    }
    if (w.type !== 'code') {
      continue;
    }
    if (w.outputs.length !== g.outputs.length) {
      say(
        at,
        'output count',
        w.outputs.map(o => o.fp),
        g.outputs.map(o => o.fp)
      );
      continue;
    }
    w.outputs.forEach((o, k) => {
      const x = g.outputs[k];
      // a stream or error preview may be longer than the fingerprint: compare the start
      const same =
        (o.fp.startsWith('text:') && x.fp.startsWith('text:')) ||
        (o.fp.startsWith('placeholder:') && x.fp.startsWith('placeholder:'))
          ? flat(x.fp).includes(flat(o.fp))
          : o.fp === x.fp;
      if (!same) {
        say(at, `output ${k} (${o.type})`, o.fp, x.fp);
      }
    });
  }
  return bad;
}
