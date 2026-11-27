/**
 * The ghost book: the shown `RebuiltState` as read-only cells, in the style of
 * the landing-page mock-up. A code cell is a rounded card: a muted `In [n]:`
 * column, the code as text, the author chip
 * and the run status; its outputs sit under a top border with `Out [n]:`.
 * Everything from history goes in as text. Nothing here runs recorded code.
 * Output rendering goes through one hook (`renderOutput`), so rich output can
 * replace the text preview later without touching the cell.
 */
import type {
  NotebookCell,
  OutputItem,
  RebuiltState,
  RunState
} from 'blaym-notebook-types';
import { chip } from './context';
import type { ContextEntry } from './context';
import { authors, UNKNOWN_AUTHOR } from './authors';
import type { CellAuthor } from './authors';
import { baseTick } from './diffbase';
import type { BaseCache } from './diffbase';
import { diff, latestOutputs } from './diff';
import type { CellMark, DiffResult } from './diff';
import type { VersionMode } from './modes';
import type { HostLive } from './host';
import type { Session } from './session';
import { edgeBanner } from './shell';
import { formatTime } from './versions';

const CHANGE_KINDS = new Set([
  'edit',
  'insert',
  'delete',
  'move',
  'type',
  'outside'
]);

const KNOWN_OUTPUT_TYPES = new Set([
  'stream',
  'display_data',
  'execute_result',
  'error'
]);

/** Draws one output item into `mount`. The default shows the preview as text. */
export type OutputRenderer = (item: OutputItem, mount: HTMLElement) => void;

export interface GhostInput {
  /** The state to show; undefined shows a calm "loading" or "nothing to show" line. */
  state?: RebuiltState;
  /** Why there is no state, when there is none. */
  unavailable?: string;
  /** One line over the cells, for example "notebook at 11-12 14:03:20 · tick 59". */
  header: string;
  /** A newer state is on its way; the cells shown are the previous ones. */
  loading: boolean;
  focusedCell: string | null;
  onFocus(cellId: string): void;
  /** Absent when the host cannot scroll its notebook (the direct page). */
  onShow?: (cellId: string) => void;
  renderOutput?: OutputRenderer;
  /** The diff against a base state, drawn over the cells. Absent: no marks. */
  diff?: GhostDiff;
  /** Who made each cell's latest change; a cell not in it is unknown. */
  authors?: ReadonlyMap<string, CellAuthor>;
  /** Calm notes over the cells: a restart, a gap, a change from outside. */
  banners?: readonly EdgeNote[];
}

export interface EdgeNote {
  /** The icon kind (see `icons.ts`). */
  kind: string;
  text: string;
}

const GAP_REASONS: Record<string, string> = {
  no_client: 'no editor was open',
  kernel_restart: 'the kernel restarted',
  server_unavailable: 'the server was not available',
  source_closed: 'the source was closed',
  switched_off: 'recording was switched off',
  unknown: 'reason not known'
};

/**
 * The edge notes for `state`. `range` is the shown version item (or the tick
 * alone); `latestChangeTick` is the tick of the latest change to the notebook,
 * and `timeAt` finds the time of a tick.
 */
export function edgeNotes(
  state: RebuiltState,
  range: { firstTick: number; lastTick: number },
  latestChangeTick: number | undefined,
  timeAt: (tick: number) => number | undefined
): EdgeNote[] {
  const notes: EdgeNote[] = [];
  const restart = state.kernel.lastRestart;
  if (restart && state.runs.some(r => r.startedTick < restart.tick)) {
    const what = restart.reason === 'crash' ? 'crashed' : 'restarted';
    notes.push({
      kind: 'kernel_restart',
      text: `kernel ${what} · outputs above are from before the ${restart.reason === 'crash' ? 'crash' : 'restart'}`
    });
  }
  for (const g of state.gaps) {
    if (
      g.startedTick <= range.lastTick &&
      (g.endedTick === undefined || g.endedTick >= range.firstTick)
    ) {
      notes.push({
        kind: 'gap',
        text: `recording gap · ${GAP_REASONS[g.reason] ?? 'reason not known'}`
      });
    }
  }
  const outside = state.lastOutsideChange;
  if (
    outside &&
    (outside.tick === latestChangeTick ||
      (outside.tick >= range.firstTick && outside.tick <= range.lastTick))
  ) {
    const ms = timeAt(outside.tick);
    notes.push({
      kind: 'outside',
      text: `changed outside the editor${ms === undefined ? '' : ` · ${formatTime(ms)}`}`
    });
  }
  return notes;
}

const AUTHOR_TEXT: Record<string, string> = {
  person: 'user',
  ai: 'AI',
  outside: 'outside',
  blaym: 'Blaym',
  unknown: 'unknown'
};

/** The cell's author chip: "user", "AI", "AI guess", "outside" or a muted "unknown". */
function authorChip(a: CellAuthor): HTMLElement {
  const text = AUTHOR_TEXT[a.author] ?? a.author;
  const guess = a.confidence === 'guess';
  const tone =
    a.author === 'person' ? 'you' : a.author === 'ai' ? 'ai' : 'plain';
  const c = chip(guess && a.author === 'ai' ? 'AI guess' : text, tone);
  if (a.author === 'unknown') {
    c.classList.add('bv-chip-unknown');
  }
  if (guess) {
    c.classList.add('bv-chip-guess');
  }
  return c;
}

/**
 * The marks over the shown cells. `against` says which way time runs: against
 * now the shown notebook is the older one ("since"); against the previous
 * version it is the newer one ("in this version").
 */
export interface GhostDiff {
  result: DiffResult;
  against: 'now' | 'previous';
}

interface Overlay {
  mark?: CellMark;
  /** A cell only the base has: a faded row at the place it had. */
  ghostRow: boolean;
  against: 'now' | 'previous';
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (className) {
    e.className = className;
  }
  if (text !== undefined) {
    e.textContent = text;
  }
  return e;
}

function sizeText(bytes: number): string {
  return bytes < 1024
    ? `${bytes} B`
    : bytes < 1024 * 1024
      ? `${(bytes / 1024).toFixed(1)} KB`
      : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The default output renderer: the preview as text, or a labelled placeholder. */
export const renderOutputText: OutputRenderer = (item, mount) => {
  mount.setAttribute('data-output-type', item.outputType);
  if (!KNOWN_OUTPUT_TYPES.has(item.outputType)) {
    mount.append(
      el('div', 'bv-placeholder', `unknown output (${item.outputType})`)
    );
  } else if (item.preview === '') {
    const kinds = item.mimeTypes.join(', ') || item.outputType;
    mount.append(el('div', 'bv-placeholder', `no text version · ${kinds}`));
  } else {
    mount.append(el('pre', 'bv-out-text', item.preview));
  }
  if (item.truncated) {
    mount.append(
      el(
        'div',
        'bv-truncated',
        `truncated · full size ${sizeText(item.sizeBytes)}`
      )
    );
  }
};

function latestRun(runs: RunState[], cellId: string): RunState | undefined {
  let best: RunState | undefined;
  for (const r of runs) {
    if (r.cellId === cellId && (!best || r.startedTick >= best.startedTick)) {
      best = r;
    }
  }
  return best;
}

function runStatus(run: RunState): HTMLElement {
  const s = el(
    'div',
    `bv-run-status bv-run-${run.status}`,
    run.codeLink === 'by_time' ? `${run.status} · matched by time` : run.status
  );
  if (run.codeLink === 'by_time') {
    s.title = 'The code was taken from the editor when the run started.';
  }
  return s;
}

/** The kind of left edge a cell gets, and the plain words for it; none when unmarked. */
function overlayClass(o: Overlay): { cls: string; note: string } | undefined {
  // Against now the shown notebook is the older: a cell only it has is gone since,
  // and a cell only the base has was added since.
  const since = o.against === 'now';
  if (o.ghostRow) {
    return since
      ? { cls: 'bv-mark-added bv-mark-ghost', note: 'added since' }
      : {
          cls: 'bv-mark-removed bv-mark-ghost',
          note: 'deleted in this version'
        };
  }
  const m = o.mark;
  if (!m) {
    return undefined;
  }
  if (m.added) {
    return since
      ? { cls: 'bv-mark-removed', note: 'deleted since' }
      : { cls: 'bv-mark-added', note: 'added in this version' };
  }
  const notes = [
    m.moved ? 'moved' : '',
    m.outputsChanged ? 'outputs changed' : ''
  ].filter(Boolean);
  return m.moved || m.codeChanged || m.outputsChanged
    ? { cls: 'bv-mark-changed', note: notes.join(' · ') }
    : undefined;
}

/** The code as text; changed lines go in their own tinted block when there are any. */
function codeBlock(
  cell: NotebookCell,
  mark: CellMark | undefined
): HTMLElement {
  const code = el('pre', 'bv-gcell-code');
  if (!mark || (mark.changedLines.length === 0 && !mark.wholeCell)) {
    code.textContent = cell.code;
    return code;
  }
  const changed = new Set(mark.changedLines);
  cell.code.split('\n').forEach((line, i) => {
    code.append(
      el(
        'span',
        mark.wholeCell || changed.has(i)
          ? 'bv-line bv-line-changed'
          : 'bv-line',
        line
      )
    );
  });
  return code;
}

function cellCard(
  cell: NotebookCell,
  state: RebuiltState,
  input: GhostInput,
  overlay?: Overlay
): HTMLElement {
  const isCode = cell.cellType === 'code';
  const card = el('div', isCode ? 'bv-gcell' : 'bv-gcell bv-gcell-text');
  card.setAttribute('data-cell-id', cell.cellId);
  card.tabIndex = 0;
  const marked = overlay ? overlayClass(overlay) : undefined;
  if (marked) {
    card.className += ` ${marked.cls}`;
  }
  if (input.focusedCell === cell.cellId) {
    card.classList.add('bv-gcell-focused');
  }
  card.addEventListener('click', () => input.onFocus(cell.cellId));
  card.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' && ev.target === card) {
      input.onFocus(cell.cellId);
    }
  });

  const run = isCode ? latestRun(state.runs, cell.cellId) : undefined;
  const count = run?.runCount ?? cell.runCount;
  const n = count === undefined ? ' ' : String(count);

  const head = el('div', 'bv-gcell-row');
  if (isCode) {
    head.append(el('span', 'bv-gcell-label', `In [${n}]:`));
  }
  head.append(codeBlock(cell, overlay?.mark));
  const side = el('div', 'bv-gcell-side');
  if (marked?.note) {
    side.append(el('span', 'bv-mark-note', marked.note));
  }
  const who = input.authors?.get(cell.cellId) ?? UNKNOWN_AUTHOR;
  const whoChip = authorChip(who);
  whoChip.setAttribute('data-author', who.author);
  side.append(whoChip);
  if (who.author === 'ai' && who.chatTurnId) {
    const prompt = el('button', 'bv-link bv-prompt-link', 'prompt \u2197');
    prompt.type = 'button';
    prompt.title = 'The prompt that made this change';
    side.append(prompt);
  }
  if (run) {
    side.append(runStatus(run));
  }
  if (input.onShow) {
    const show = el('button', 'bv-link', 'show in notebook');
    show.type = 'button';
    show.addEventListener('click', ev => {
      ev.stopPropagation();
      input.onShow?.(cell.cellId);
    });
    side.append(show);
  }
  head.append(side);
  card.append(head);

  const out = isCode ? latestOutputs(cell) : undefined;
  if (out) {
    const outRun = state.runs.find(r => r.runId === out.runId);
    const outN = outRun?.runCount ?? cell.runCount;
    const row = el('div', 'bv-gcell-row bv-gcell-out');
    row.append(
      el('span', 'bv-gcell-label', `Out [${outN === undefined ? ' ' : outN}]:`)
    );
    const items = el('div', 'bv-out-items');
    const draw = input.renderOutput ?? renderOutputText;
    for (const item of out.items) {
      const mount = el('div', 'bv-out-item');
      mount.setAttribute('data-content-hash', item.contentHash);
      draw(item, mount);
      items.append(mount);
    }
    row.append(items);
    card.append(row);
    // Outputs that came with a paste stay marked until the cell's own first run.
    if (
      out.runId.startsWith('pasted:') &&
      !state.runs.some(r => r.cellId === cell.cellId)
    ) {
      items.append(el('div', 'bv-copied-note', 'output copied with the cell'));
    }
  }
  return card;
}

/** Draws `input` into `target`, replacing what was there. */
export function renderGhostBook(target: HTMLElement, input: GhostInput): void {
  const book = el('div', 'bv-book');
  book.classList.toggle('bv-book-loading', input.loading);
  const head = el('div', 'bv-book-head');
  head.append(el('span', 'bv-label', input.header));
  if (input.loading) {
    const tag = el('span', 'bv-loading-tag', 'loading');
    tag.setAttribute('role', 'status');
    head.append(tag);
  }
  book.append(head);
  if (input.banners?.length) {
    const notes = el('div', 'bv-banners');
    for (const n of input.banners) {
      notes.append(edgeBanner(n.kind, n.text));
    }
    book.append(notes);
  }
  const cells = el('div', 'bv-book-cells');
  if (!input.state) {
    cells.append(
      el('p', 'bv-muted', input.unavailable ?? 'Loading the notebook…')
    );
  } else if (input.state.notebook.cells.length === 0) {
    cells.append(
      el('p', 'bv-muted', 'The notebook had no cells at this moment.')
    );
  } else {
    const state = input.state;
    const d = input.diff;
    const rows: { cell: NotebookCell; ghostRow: boolean }[] =
      state.notebook.cells.map(cell => ({ cell, ghostRow: false }));
    // Cells only the base has go back at the index they had there (base order).
    for (const gone of d?.result.deleted ?? []) {
      rows.splice(Math.min(gone.index, rows.length), 0, {
        cell: gone.cell,
        ghostRow: true
      });
    }
    for (const row of rows) {
      cells.append(
        cellCard(
          row.cell,
          state,
          input,
          d && {
            mark: d.result.cells.get(row.cell.cellId),
            ghostRow: row.ghostRow,
            against: d.against
          }
        )
      );
    }
  }
  book.append(cells);
  const top = target.scrollTop;
  target.replaceChildren(book);
  target.scrollTop = top;
}

export interface Ghost {
  /** The timeline index, for the time in the header. */
  setEntries(entries: readonly ContextEntry[]): void;
}

/** Keeps `target` showing the session's shown state, following the focus. */
export function createGhost(
  target: HTMLElement,
  session: Session,
  live?: HostLive,
  renderOutput?: OutputRenderer,
  diffs?: { cache: BaseCache; mode: VersionMode }
): Ghost {
  let entries: readonly ContextEntry[] = [];

  const timeAt = (tick: number): number | undefined => {
    let found: ContextEntry | undefined;
    for (const e of entries) {
      if (e.tick > tick) {
        break;
      }
      found = e;
    }
    return found?.timeMs;
  };

  function draw(): void {
    const view = session.view();
    const result = view.shown?.result;
    const ok = result && result.status === 'ok' ? result.state : undefined;
    const tick = ok?.position.lastEventTick ?? view.shown?.tick;
    let header = 'notebook';
    if (tick !== undefined) {
      const ms = view.mode === 'lookingBack' ? timeAt(tick) : undefined;
      header =
        view.mode === 'lookingBack'
          ? `notebook at ${ms === undefined ? '…' : formatTime(ms)} · tick ${tick}`
          : `notebook now · tick ${tick}`;
    }
    const against = session.focus.get().diffBase;
    const wanted = baseTick(against, view, entries, diffs?.mode ?? 'grouped');
    let marks: GhostDiff | undefined;
    if (diffs && ok && wanted !== undefined) {
      const base = diffs.cache.peek(view.branchId, wanted);
      if (base) {
        marks = {
          result: diff(base, ok),
          against: wanted > (view.tick ?? wanted) ? 'now' : 'previous'
        };
      } else {
        // The marks arrive when the base has loaded; the cells are not redrawn without them.
        void diffs.cache.load(view.branchId, wanted).then(loaded => {
          if (loaded) {
            draw();
          }
        });
      }
    }
    const shownAt = tick ?? 0;
    const selected = session.focus.get().selected;
    const range =
      selected && selected.firstTick <= shownAt
        ? { firstTick: selected.firstTick, lastTick: shownAt }
        : { firstTick: shownAt, lastTick: shownAt };
    let latestChange: number | undefined;
    for (const e of entries) {
      if (e.tick > shownAt) {
        break;
      }
      if (CHANGE_KINDS.has(e.kind)) {
        latestChange = e.tick;
      }
    }
    renderGhostBook(target, {
      authors: authors(entries, shownAt),
      banners: ok ? edgeNotes(ok, range, latestChange, timeAt) : undefined,
      diff: marks,
      state: ok,
      unavailable:
        result && result.status !== 'ok'
          ? 'No notebook state could be built for this moment.'
          : undefined,
      header,
      loading: view.freshness === 'loading',
      focusedCell: session.focus.get().focusedCell,
      onFocus: cellId => session.focus.focusCell(cellId),
      onShow: live ? cellId => live.scrollToCell(cellId) : undefined,
      renderOutput
    });
  }

  let drawnFocus = session.focus.get().focusedCell;
  session.subscribe(draw);
  // Hover and selection changes do not touch the cells; only the focused cell does.
  session.focus.subscribe(f => {
    if (f.focusedCell !== drawnFocus) {
      drawnFocus = f.focusedCell;
      draw();
    }
  });
  draw();
  return {
    setEntries(next) {
      entries = next;
      draw();
    }
  };
}
