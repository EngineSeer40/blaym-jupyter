/**
 * The context panel: the details of the selected moment. It reads one timeline
 * entry and the state at that tick, nothing else: no editor- or
 * source-specific branching.
 */
import type {
  AuthorConfidence,
  AuthorHint,
  RebuiltState,
  TimelineIndexResponse
} from 'blaym-notebook-types';
import { isKnownKind, renderIcon } from './icons';
import { edgeBanner } from './shell';
import { promptSection } from './conversation';

/** One row of the timeline index. `kind` is a string: it may be one this viewer does not know. */
export interface ContextEntry {
  tick: number;
  timeMs: number;
  kind: string;
  author: AuthorHint;
  confidence: AuthorConfidence | null;
  cellId: string | null;
  runId: string | null;
  /** The cell's place in the notebook's cell order and the cell count, when the answer has them. */
  cellIndex?: number | null;
  cellCount?: number;
}

export function entryAt(index: TimelineIndexResponse, i: number): ContextEntry {
  return {
    tick: index.ticks[i],
    timeMs: index.timesMs[i],
    kind: index.kinds[i],
    author: index.authors[i],
    confidence: index.confidences[i],
    cellId: index.cellIds[i],
    runId: index.runIds[i],
    ...(index.cellIndexes && index.cellCounts
      ? {
          cellIndex: index.cellIndexes[i],
          cellCount: index.cellCounts[i]
        }
      : {})
  };
}

export const LABELS: Record<string, string> = {
  edit: 'Code edit',
  insert: 'Cell added',
  delete: 'Cell deleted',
  move: 'Cell moved',
  type: 'Cell type changed',
  run_started: 'Run started',
  run_ok: 'Run finished',
  run_error: 'Run failed',
  run_interrupted: 'Run interrupted',
  chart: 'Chart',
  table: 'Table',
  text_output: 'Text output',
  chat_prompt: 'Chat prompt',
  chat_reply: 'Chat reply',
  kernel_start: 'Kernel started',
  kernel_restart: 'Kernel restarted',
  outside: 'Changed outside Jupyter',
  gap: 'Recording gap',
  branch_point: 'Branch point',
  snapshot: 'Snapshot',
  other: 'Other change'
};

const AUTHORS: Record<AuthorHint, string> = {
  person: 'user',
  ai: 'AI',
  outside: 'outside',
  blaym: 'Blaym',
  unknown: 'unknown'
};

/** An insert that arrived with code or outputs is a paste (or an AI insert), read from the state at its tick. */
function pastedLabel(
  entry: ContextEntry,
  state: RebuiltState | undefined
): string | undefined {
  if (entry.kind !== 'insert' || !state) {
    return undefined;
  }
  const cell = state.notebook.cells.find(c => c.cellId === entry.cellId);
  if (!cell) {
    return undefined;
  }
  if (cell.outputs.some(o => o.items.length > 0)) {
    return 'Pasted cell (with output)';
  }
  return cell.code.trim() === '' ? undefined : 'Pasted cell';
}

const CHANGE_KINDS = new Set(['edit', 'insert', 'delete', 'move', 'type']);

const plain = (s: string) => s.replace(/_/g, ' ');

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

export function chip(text: string, tone: string): HTMLElement {
  return el('span', `bv-chip bv-chip-${tone}`, text);
}

function authorChips(entry: ContextEntry): HTMLElement[] {
  const tone =
    entry.author === 'person' ? 'you' : entry.author === 'ai' ? 'ai' : 'plain';
  const chips = [chip(AUTHORS[entry.author], tone)];
  if (entry.confidence === 'guess') {
    chips.push(chip('guess', 'plain'));
  }
  return chips;
}

function row(list: HTMLElement, label: string, value: string | HTMLElement) {
  list.append(el('dt', 'bv-label', label));
  const dd = el('dd', 'bv-value');
  dd.append(value);
  list.append(dd);
}

function formatTime(ms: number): string {
  return new Date(ms).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

/** The entry's fields as the index gave them, for a kind this viewer does not know. */
function fieldList(entry: ContextEntry): [string, string][] {
  return Object.entries(entry).map(([k, v]) => [
    k,
    v === null || v === undefined ? 'none' : String(v)
  ]);
}

/** Fills `target` with the details of `entry`, or the empty message. `state` is the state at its tick; `chatTurnId` is the event's `links.chatTurnId`, when it has one. */
export function renderContext(
  target: HTMLElement,
  entry: ContextEntry | null,
  state?: RebuiltState,
  chatTurnId?: string | null
): void {
  target.replaceChildren();
  const heading = el('div', 'bv-label bv-heading', 'selected moment');
  target.append(heading);
  if (!entry) {
    target.append(
      el('p', 'bv-muted', 'Select a moment in the history to see its details.')
    );
    return;
  }
  const known = isKnownKind(entry.kind);
  const title = el('div', 'bv-context-title');
  title.append(renderIcon(entry.kind, entry.author, entry.confidence, 32));
  title.append(
    el(
      'span',
      'bv-context-kind',
      known ? (pastedLabel(entry, state) ?? LABELS[entry.kind]) : entry.kind
    )
  );
  title.append(...authorChips(entry));
  target.append(title);

  if (!known) {
    target.append(
      el(
        'p',
        'bv-muted',
        'This viewer does not know this kind of step. It is shown as a generic step.'
      )
    );
  }

  const gap =
    entry.kind === 'gap'
      ? state?.gaps.find(
          g =>
            g.startedTick === entry.tick ||
            (g.endedTick !== undefined && g.endedTick === entry.tick)
        )
      : undefined;
  if (entry.kind === 'kernel_restart') {
    const last = state?.kernel.lastRestart;
    target.append(
      edgeBanner(
        'kernel_restart',
        last && last.tick === entry.tick
          ? `kernel restarted · ${plain(last.reason)}`
          : 'kernel restarted'
      )
    );
  } else if (entry.kind === 'outside') {
    target.append(edgeBanner('outside', 'changed outside Jupyter'));
  } else if (entry.kind === 'gap') {
    target.append(
      edgeBanner(
        'gap',
        gap
          ? `not recorded · ${gap.source} · ${plain(gap.reason)}`
          : 'not recorded'
      )
    );
  }

  const list = el('dl', 'bv-fields');
  if (known) {
    row(list, 'time', formatTime(entry.timeMs));
    row(list, 'tick', String(entry.tick));
    const run = entry.runId
      ? state?.runs.find(r => r.runId === entry.runId)
      : undefined;
    const cell = entry.cellId ?? run?.cellId;
    if (cell) {
      row(list, 'cell', cell);
    }
    if (entry.runId) {
      row(list, 'run', entry.runId);
    }
    if (run) {
      const status = el('span');
      status.append(plain(run.status));
      if (run.codeLink === 'by_time') {
        status.append(' ', chip('matched by time', 'plain'));
      }
      row(list, 'run status', status);
    }
    if (gap) {
      row(list, 'gap source', gap.source);
      row(list, 'gap reason', plain(gap.reason));
    }
  } else {
    for (const [k, v] of fieldList(entry)) {
      row(list, k, v);
    }
  }
  target.append(list);
  if (CHANGE_KINDS.has(entry.kind)) {
    target.append(promptSection(state?.chat, chatTurnId));
  }
}
