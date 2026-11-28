/**
 * The version list: the items of `modes.ts`, newest at the top. Click or
 * Up/Down selects an item and goes to its last tick (looking back); "back to
 * now" returns to following. Only the visible rows are in the page (a simple
 * windowing at a fixed row height). Reads the entries and the focus state; changes
 * nothing but through the session.
 */
import type { ContextEntry } from './context';
import type { Session } from './session';
import { renderIcon } from './icons';
import { versions as versionItems } from './modes';
import type { VersionItem, VersionMode } from './modes';
import type { CellNamer } from './cellname';

export const ROW_HEIGHT = 46;
const OVERSCAN = 4;
const FALLBACK_HEIGHT = 600;

export interface VersionList {
  setEntries(entries: readonly ContextEntry[]): void;
  /** Draws again, for when the list has just become visible. */
  refresh(): void;
}

/** What the "changes" view adds to the shared list: another title and a part at the end of each row. */
export interface VersionListOptions {
  title?: string;
  decorate?: (item: VersionItem) => Element | null;
  /** How a row names its cell; the default shows the id. */
  cellName?: CellNamer;
}

const AUTHOR_TEXT: Record<string, string> = {
  person: 'user',
  ai: 'AI',
  outside: 'outside',
  blaym: 'Blaym',
  unknown: 'unknown'
};

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

export function formatTime(ms: number): string {
  return new Date(ms).toISOString().slice(5, 19).replace('T', ' ');
}

const QUIET_UNKNOWN = /^(run_|chat_|kernel_|snapshot$)/;

/** The author chip; absent for an unknown author on run, chat, snapshot and kernel rows. */
function authorChip(item: VersionItem): HTMLElement | null {
  if (item.author === 'unknown' && QUIET_UNKNOWN.test(item.kind)) {
    return null;
  }
  const tone =
    item.author === 'person' ? 'you' : item.author === 'ai' ? 'ai' : 'plain';
  const text = AUTHOR_TEXT[item.author] ?? item.author;
  return el(
    'span',
    `bv-chip bv-chip-${tone}`,
    item.confidence === 'guess' ? `${text} · guess` : text
  );
}

export function createVersions(
  target: HTMLElement,
  session: Session,
  initialMode: VersionMode = 'grouped',
  options: VersionListOptions = {}
): VersionList {
  let entries: readonly ContextEntry[] = [];
  let mode: VersionMode = initialMode;
  let newestFirst: VersionItem[] = [];
  // The selection the list last scrolled to, so scrolling by hand is not undone.
  let scrolledTo: number | null = null;

  target.replaceChildren();
  const head = el('div', 'bv-versions-head');
  const title = el('div', 'bv-label', options.title ?? 'versions');
  const now = el('button', 'bv-button', 'back to now');
  now.type = 'button';
  const switcher = el('div', 'bv-segmented');
  switcher.setAttribute('role', 'group');
  switcher.setAttribute('aria-label', 'version list mode');
  const buttons = new Map<VersionMode, HTMLButtonElement>();
  for (const [m, label] of [
    ['grouped', 'grouped'],
    ['every', 'every step'],
    ['sessions', 'by session']
  ] as const) {
    const b = el('button', 'bv-segment', label);
    b.type = 'button';
    b.addEventListener('click', () => setMode(m));
    buttons.set(m, b);
    switcher.append(b);
  }
  const top = el('div', 'bv-versions-top');
  top.append(title, now);
  head.append(top, switcher);
  const list = el('div', 'bv-version-list');
  list.tabIndex = 0;
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', 'versions');
  const inner = el('div', 'bv-version-inner');
  list.append(inner);
  const empty = el('p', 'bv-muted bv-version-empty', 'No history yet.');
  target.append(head, list);

  const selectedIndex = (): number => {
    const sel = session.focus.get().selected;
    return sel
      ? newestFirst.findIndex(
          i => i.firstTick <= sel.firstTick && sel.firstTick <= i.lastTick
        )
      : -1;
  };

  function choose(item: VersionItem): void {
    session.focus.select({
      firstTick: item.firstTick,
      lastTick: item.lastTick,
      kind: item.kind
    });
    session.focus.goto(session.view().branchId, item.lastTick);
  }

  function row(item: VersionItem, i: number, selected: boolean): HTMLElement {
    const r = el('div', 'bv-version');
    r.setAttribute('role', 'option');
    r.setAttribute('aria-selected', String(selected));
    if (selected) {
      r.classList.add('bv-version-selected');
    }
    r.style.setProperty('top', `${i * ROW_HEIGHT}px`);
    r.style.setProperty('height', `${ROW_HEIGHT}px`);
    const text = el('div', 'bv-version-text');
    // A session row has no one author or time: its span on the first line, its counts on the second.
    const cut = item.summary.lastIndexOf(' · ');
    const isSession = item.kind === 'session' && cut > 0;
    text.append(
      el(
        'div',
        'bv-version-summary',
        isSession ? item.summary.slice('session · '.length, cut) : item.summary
      ),
      el(
        'div',
        'bv-version-time',
        isSession ? item.summary.slice(cut + 3) : formatTime(item.timeMs)
      )
    );
    if (isSession) {
      r.title = item.summary;
    }
    r.append(renderIcon(item.kind, item.author, item.confidence, 22), text);
    const chip = item.kind === 'session' ? null : authorChip(item);
    if (chip) {
      r.append(chip);
    }
    const extra = options.decorate?.(item);
    if (extra) {
      r.append(extra);
    }
    r.addEventListener('click', () => {
      list.focus({ preventScroll: true });
      choose(item);
    });
    return r;
  }

  function draw(): void {
    now.disabled = session.view().mode === 'following';
    for (const [m, b] of buttons) {
      b.setAttribute('aria-pressed', String(m === mode));
    }
    if (newestFirst.length === 0) {
      inner.replaceChildren(empty);
      inner.style.setProperty('height', 'auto');
      return;
    }
    const height = list.clientHeight || FALLBACK_HEIGHT;
    const sel = session.focus.get().selected;
    const at = selectedIndex();
    if (!sel) {
      scrolledTo = null;
    }
    if (sel && at >= 0 && scrolledTo !== sel.firstTick) {
      scrolledTo = sel.firstTick;
      const top = at * ROW_HEIGHT;
      if (top < list.scrollTop) {
        list.scrollTop = top;
      } else if (top + ROW_HEIGHT > list.scrollTop + height) {
        list.scrollTop = top + ROW_HEIGHT - height;
      }
    }
    const first = Math.max(
      0,
      Math.floor(list.scrollTop / ROW_HEIGHT) - OVERSCAN
    );
    const last = Math.min(
      newestFirst.length,
      Math.ceil((list.scrollTop + height) / ROW_HEIGHT) + OVERSCAN
    );
    const selected = at;
    inner.style.setProperty('height', `${newestFirst.length * ROW_HEIGHT}px`);
    inner.replaceChildren(
      ...newestFirst
        .slice(first, last)
        .map((item, k) => row(item, first + k, first + k === selected))
    );
  }

  function rebuild(): void {
    newestFirst = versionItems(entries, {
      mode,
      cellName: options.cellName
    }).reverse();
    draw();
  }

  function setMode(next: VersionMode): void {
    if (next !== mode) {
      mode = next;
      scrolledTo = null;
      list.scrollTop = 0;
      rebuild();
    }
  }

  function move(delta: number): void {
    if (newestFirst.length === 0) {
      return;
    }
    const at = selectedIndex();
    const to =
      at < 0
        ? delta > 0
          ? 0
          : newestFirst.length - 1
        : Math.min(newestFirst.length - 1, Math.max(0, at + delta));
    if (to === at) {
      return;
    }
    const top = to * ROW_HEIGHT;
    const height = list.clientHeight || FALLBACK_HEIGHT;
    if (top < list.scrollTop) {
      list.scrollTop = top;
    } else if (top + ROW_HEIGHT > list.scrollTop + height) {
      list.scrollTop = top + ROW_HEIGHT - height;
    }
    choose(newestFirst[to]);
  }

  list.addEventListener('keydown', ev => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      move(ev.key === 'ArrowDown' ? 1 : -1);
    }
  });
  list.addEventListener('scroll', draw);
  now.addEventListener('click', () => {
    session.focus.select(null);
    session.backToNow();
  });
  session.focus.subscribe(draw);
  session.subscribe(draw);
  rebuild();

  return {
    setEntries(next) {
      const before = newestFirst.length;
      entries = next;
      newestFirst = versionItems(entries, {
        mode,
        cellName: options.cellName
      }).reverse();
      // New items come in at the top: keep the rows the user is looking at in place.
      if (before > 0 && list.scrollTop > 0) {
        list.scrollTop += (newestFirst.length - before) * ROW_HEIGHT;
      }
      draw();
    },
    refresh: draw
  };
}
