/**
 * View modes: pure functions from the timeline index to the version list's
 * items. "grouped" is the default: a run is one item, a structural change or an
 * edge is one item, and consecutive edits to one cell merge. "every" lists every
 * timeline entry.
 */
import type { AuthorConfidence, AuthorHint } from 'blaym-notebook-types';
import type { ContextEntry } from './context';
import type { CellNamer } from './cellname';

export type VersionMode = 'grouped' | 'every';

/** One changed cell of an item: where it sits (0 to 1 along the notebook) and how it changed. */
export interface ChangeMark {
  /** Position along the notebook's cells, 0 to 1. */
  at: number;
  tone: 'added' | 'removed' | 'changed';
}

export interface VersionItem {
  firstTick: number;
  lastTick: number;
  /** The kind the row's icon shows: for a run, its latest state. */
  kind: string;
  summary: string;
  cells: string[];
  runs: string[];
  authors: AuthorHint[];
  /** The item's latest author and its confidence. */
  author: AuthorHint;
  confidence: AuthorConfidence | null;
  /** The time of the item's last entry. */
  timeMs: number;
  /** How many index entries the item stands for. */
  count: number;
  /** Where the item's entries changed cells; empty when the index has no cell positions. */
  marks: ChangeMark[];
  /** Where the first cell sat in the notebook when its first entry happened, if the index says. */
  cellIndex?: number | null;
}

const TONE: Record<string, ChangeMark['tone']> = {
  insert: 'added',
  delete: 'removed',
  edit: 'changed',
  type: 'changed',
  move: 'changed',
  outside: 'changed'
};

/** The mark of one entry: none for runs, outputs and entries without a cell position. */
function markOf(e: ContextEntry): ChangeMark | null {
  const tone = TONE[e.kind];
  if (!tone || e.cellIndex == null || !e.cellCount) {
    return null;
  }
  // A delete's index is from before the change, so the notebook then had one more cell.
  const n = e.cellCount + (tone === 'removed' ? 1 : 0);
  return { at: (e.cellIndex + 0.5) / Math.max(n, e.cellIndex + 1), tone };
}

function addMark(item: VersionItem, e: ContextEntry): void {
  const mark = markOf(e);
  const key = (x: ChangeMark) => `${x.tone}${Math.round(x.at * 1000)}`;
  if (mark && !item.marks.some(x => key(x) === key(mark))) {
    item.marks.push(mark);
  }
}

const RUN_KINDS = new Set([
  'run_started',
  'run_ok',
  'run_error',
  'run_interrupted'
]);
const OUTPUT_KINDS = new Set(['chart', 'table', 'text_output']);
const RUN_RESULT: Record<string, string> = {
  run_started: 'running',
  run_ok: 'ok',
  run_error: 'error',
  run_interrupted: 'interrupted'
};

const TEXT: Record<string, string> = {
  insert: 'added cell',
  delete: 'deleted cell',
  move: 'moved cell',
  type: 'changed type of cell',
  chart: 'chart',
  table: 'table',
  text_output: 'text output',
  chat_prompt: 'chat prompt',
  chat_reply: 'chat reply',
  kernel_start: 'kernel started',
  kernel_restart: 'kernel restarted',
  outside: 'changed outside Jupyter',
  gap: 'recording gap',
  branch_point: 'branch point',
  snapshot: 'snapshot',
  other: 'other change'
};

/** The verb in front of a cell's name; without a cell, the kind's own text is used. */
const CELL_VERB: Record<string, string> = {
  insert: 'added',
  delete: 'deleted',
  move: 'moved',
  type: 'changed type of'
};

const KNOWN = new Set([
  'edit',
  ...RUN_KINDS,
  ...OUTPUT_KINDS,
  ...Object.keys(TEXT)
]);

function itemOf(e: ContextEntry): VersionItem {
  const item: VersionItem = {
    firstTick: e.tick,
    lastTick: e.tick,
    kind: e.kind,
    summary: '',
    cells: e.cellId ? [e.cellId] : [],
    runs: e.runId ? [e.runId] : [],
    authors: [e.author],
    author: e.author,
    confidence: e.confidence,
    timeMs: e.timeMs,
    count: 1,
    marks: [],
    ...(e.cellIndex === undefined ? {} : { cellIndex: e.cellIndex })
  };
  addMark(item, e);
  return item;
}

function extend(item: VersionItem, e: ContextEntry): void {
  item.lastTick = e.tick;
  item.timeMs = e.timeMs;
  item.author = e.author;
  item.confidence = e.confidence;
  item.count += 1;
  addMark(item, e);
  if (e.cellId && !item.cells.includes(e.cellId)) {
    item.cells.push(e.cellId);
  }
  if (e.runId && !item.runs.includes(e.runId)) {
    item.runs.push(e.runId);
  }
  if (!item.authors.includes(e.author)) {
    item.authors.push(e.author);
  }
}

const RUN_STEP: Record<string, string> = {
  run_started: 'run started',
  run_ok: 'run finished',
  run_error: 'run failed',
  run_interrupted: 'run interrupted'
};

function summarise(
  item: VersionItem,
  lastKind: string,
  single: boolean,
  name: CellNamer
): string {
  const id = item.cells[0];
  const cell = id ? name(id, item.cellIndex) : undefined;
  const of = (verb: string) =>
    cell ? `${verb} ${cell}` : (TEXT[item.kind] ?? verb);
  if (item.kind === 'edit') {
    const n = item.count > 1 ? ` (${item.count} edits)` : '';
    return cell ? `edited ${cell}${n}` : `edited a cell${n}`;
  }
  if (single && RUN_KINDS.has(item.kind)) {
    return cell ? `${RUN_STEP[item.kind]} (${cell})` : RUN_STEP[item.kind];
  }
  if (RUN_KINDS.has(item.kind)) {
    const target = cell ? `ran ${cell}` : 'ran a cell';
    return `${target}: ${RUN_RESULT[item.kind]}`;
  }
  if (TEXT[item.kind]) {
    const text = TEXT[item.kind];
    const verb = CELL_VERB[item.kind];
    return verb ? of(verb) : text;
  }
  return `unknown: ${lastKind}`;
}

/** Appends `incoming` (oldest first) to `existing`, ignoring ticks already held. `gap` is true when the first new tick does not follow the last held one: the caller refetches everything rather than guess. */
export function appendEntries(
  existing: readonly ContextEntry[],
  incoming: readonly ContextEntry[]
): { entries: ContextEntry[]; gap: boolean } {
  const entries = existing.slice();
  let last = entries.length ? entries[entries.length - 1].tick : undefined;
  for (const e of incoming) {
    if (last !== undefined && e.tick <= last) {
      continue;
    }
    if (last !== undefined && e.tick !== last + 1) {
      return { entries: existing.slice(), gap: true };
    }
    entries.push(e);
    last = e.tick;
  }
  return { entries, gap: false };
}

/** The list's items in time order (oldest first); the list renders them newest first. */
export function versions(
  index: readonly ContextEntry[],
  options: { mode: VersionMode; cellName?: CellNamer }
): VersionItem[] {
  const items: VersionItem[] = [];
  const kinds: string[] = [];
  const push = (item: VersionItem, kind: string) => {
    items.push(item);
    kinds.push(kind);
  };
  const runItems = new Map<string, VersionItem>();
  for (const e of index) {
    if (options.mode === 'every') {
      push(itemOf(e), e.kind);
      continue;
    }
    const last = items[items.length - 1];
    if (e.kind === 'edit' && last && last.kind === 'edit') {
      const sameCell = e.cellId !== null && last.cells[0] === e.cellId;
      if (sameCell) {
        extend(last, e);
        continue;
      }
    }
    const isRun = RUN_KINDS.has(e.kind);
    const runItem = e.runId ? runItems.get(e.runId) : undefined;
    if (runItem && (isRun || OUTPUT_KINDS.has(e.kind))) {
      extend(runItem, e);
      if (isRun) {
        runItem.kind = e.kind;
      }
      continue;
    }
    const item = itemOf(e);
    if (isRun && e.runId) {
      runItems.set(e.runId, item);
    }
    push(item, e.kind);
  }
  items.forEach((item, i) => {
    item.summary = summarise(
      item,
      kinds[i],
      options.mode === 'every',
      options.cellName ?? (id => `cell ${id}`)
    );
    if (options.mode === 'every' && !KNOWN.has(item.kind)) {
      item.summary = `unknown: ${item.kind}`;
    }
  });
  return items;
}
