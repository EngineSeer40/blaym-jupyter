/**
 * The "search" view of the side panel: the hits of the query in the header
 * field, newest first, one row per (branch, tick, kind). Typing waits for a
 * short pause before asking. Clicking a hit looks back at that moment (and at
 * the hit's cell when the timeline entry names one). Snippets go in as text.
 */
import type { SearchHit, SearchHitKind } from 'blaym-notebook-types';
import type { Client } from './client';
import type { ContextEntry } from './context';
import type { Session } from './session';
import type { Timelines } from './timelines';
import { renderIcon } from './icons';
import { ROW_HEIGHT, formatTime } from './versions';

export const SEARCH_PAUSE_MS = 250;
const SEARCH_LIMIT = 50;

/** The timeline kind whose icon stands for each kind of hit. */
const HIT_ICON: Record<SearchHitKind, string> = {
  code: 'edit',
  chat: 'chat_prompt',
  output_text: 'text_output',
  table_columns: 'table',
  chart_text: 'chart'
};

const HIT_LABEL: Record<SearchHitKind, string> = {
  code: 'code',
  chat: 'chat',
  output_text: 'output',
  table_columns: 'table columns',
  chart_text: 'chart text'
};

export interface HitRow {
  branchId: string;
  tick: number;
  kind: SearchHitKind;
  snippet: string;
  /** The cell the moment's timeline entry names, and its time, when the entry is known. */
  cellId: string | null;
  timeMs: number | null;
}

/** Folds hits that share a (branch, tick, kind) into one row; newest (highest tick) first. */
export function hitRows(
  hits: readonly SearchHit[],
  entryOf: (branchId: string, tick: number) => ContextEntry | undefined
): HitRow[] {
  const seen = new Map<string, HitRow>();
  for (const h of hits) {
    const key = `${h.branchId}|${h.tick}|${h.kind}`;
    if (!seen.has(key)) {
      const entry = entryOf(h.branchId, h.tick);
      seen.set(key, {
        branchId: h.branchId,
        tick: h.tick,
        kind: h.kind,
        snippet: h.snippet,
        cellId: entry?.cellId ?? null,
        timeMs: entry?.timeMs ?? null
      });
    }
  }
  return [...seen.values()].sort((a, b) => b.tick - a.tick);
}

export interface SearchView {
  /** The text of the header field; an empty text clears the view. */
  setQuery(text: string): void;
}

export interface SearchOptions {
  client: Pick<Client, 'search'>;
  explorationId: string;
  session: Pick<Session, 'focus' | 'view'>;
  timelines: Pick<Timelines, 'own' | 'branches'>;
  scheduler: { setTimeout(fn: () => void, ms: number): () => void };
}

function el(className: string, text?: string): HTMLElement {
  const e = document.createElement('div');
  e.className = className;
  if (text !== undefined) {
    e.textContent = text;
  }
  return e;
}

export function createSearch(
  target: HTMLElement,
  o: SearchOptions
): SearchView {
  let cancel: (() => void) | undefined;
  let asked = 0;
  const head = el('bv-versions-head');
  head.append(el('div bv-label', 'search'));
  const list = el('bv-version-list');
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', 'search hits');
  const inner = el('bv-version-inner');
  list.append(inner);
  target.replaceChildren(head, list);

  const note = (text: string) => {
    const p = el('bv-muted bv-version-empty', text);
    p.setAttribute('role', 'status');
    inner.style.setProperty('height', 'auto');
    inner.replaceChildren(p);
  };

  // A branch without a name of its own (the root's name is its id) shows "main" or a short id.
  const branchName = (id: string): string => {
    const b = o.timelines.branches().find(x => x.branchId === id);
    return b?.name && b.name !== id
      ? b.name
      : b && !b.parentBranchId
        ? 'main'
        : id.slice(0, 8);
  };

  const entryOf = (branchId: string, tick: number) =>
    o.timelines.own(branchId).find(e => e.tick === tick);

  function choose(row: HitRow): void {
    o.session.focus.select({
      firstTick: row.tick,
      lastTick: row.tick,
      kind: HIT_ICON[row.kind]
    });
    o.session.focus.goto(row.branchId, row.tick);
    if (row.cellId) {
      o.session.focus.focusCell(row.cellId);
    }
  }

  function draw(rows: HitRow[], query: string): void {
    if (rows.length === 0) {
      note(`nothing found for “${query}”`);
      return;
    }
    inner.style.setProperty('height', `${rows.length * ROW_HEIGHT}px`);
    inner.replaceChildren(
      ...rows.map((r, i) => {
        const row = el('bv-version');
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', 'false');
        row.style.setProperty('top', `${i * ROW_HEIGHT}px`);
        row.style.setProperty('height', `${ROW_HEIGHT}px`);
        const text = el('bv-version-text');
        text.append(
          el('bv-version-summary', r.snippet),
          el(
            'bv-version-time',
            [
              HIT_LABEL[r.kind],
              r.timeMs === null ? null : formatTime(r.timeMs),
              branchName(r.branchId)
            ]
              .filter(Boolean)
              .join(' · ')
          )
        );
        row.append(renderIcon(HIT_ICON[r.kind], null, null, 22), text);
        row.addEventListener('click', () => choose(r));
        return row;
      })
    );
  }

  function ask(query: string, mine: number): void {
    o.client
      .search({
        explorationId: o.explorationId,
        query,
        limit: SEARCH_LIMIT
      })
      .then(
        res => mine === asked && draw(hitRows(res.hits, entryOf), query),
        () => mine === asked && note('search is unavailable')
      );
  }

  return {
    setQuery(text) {
      cancel?.();
      const query = text.trim();
      const mine = ++asked;
      if (query === '') {
        inner.replaceChildren();
        return;
      }
      note('searching…');
      cancel = o.scheduler.setTimeout(() => ask(query, mine), SEARCH_PAUSE_MS);
    }
  };
}
