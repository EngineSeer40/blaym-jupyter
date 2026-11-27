import './viewer.css';
import { createClient } from './client';
import type { Backend } from './client';
import { createHostChannel, windowPort } from './host';
import { createBaseCache } from './diffbase';
import { createGhost } from './ghost';
import { createOutputRenderer } from './outputs';
import { entryAt, renderContext } from './context';
import type { ContextEntry } from './context';
import { appendEntries } from './modes';
import { createSession } from './session';
import { createChanges } from './changes';
import { cellName } from './cellname';
import { renderConversation } from './conversation';
import { createShell } from './shell';
import type { PanelView, ShellStatus } from './shell';
import { startTheme } from './theme';

/** The address fragment: `#explorationId=..&mode=direct|host&base=..&branch=..&orientation=..`,
 * and optionally `versions=every` (the version list's mode), `view=changes` and `panel=collapsed|open` (the side panel), `tick=` (open looking back at that tick) and `select=` (select the timeline entry at that tick). */
function readFragment(): URLSearchParams {
  return new URLSearchParams(window.location.hash.replace(/^#/, ''));
}

const scheduler = {
  setTimeout: (fn: () => void, ms: number) => {
    const handle = window.setTimeout(fn, ms);
    return () => window.clearTimeout(handle);
  }
};

const hostPort = windowPort();
startTheme(window, hostPort);

const params = readFragment();
const explorationId = params.get('explorationId') ?? '';
const mode = params.get('mode') === 'direct' ? 'direct' : 'host';
const channel =
  mode === 'direct'
    ? undefined
    : createHostChannel({ port: hostPort, scheduler });
const backend: Backend = channel
  ? { kind: 'host', channel }
  : { kind: 'direct', base: params.get('base') ?? '' };

const client = createClient({ backend, scheduler });
const session = createSession({
  client,
  explorationId,
  activeBranchId: params.get('branch') ?? ''
});

/** The remembered side panel choices; the webview may block local storage, so every use is guarded. */
const STORE_KEY = 'blaym-panel';
function recall(): { view?: string; collapsed?: boolean } {
  try {
    return JSON.parse(window.localStorage.getItem(STORE_KEY) ?? '{}');
  } catch {
    return {};
  }
}
function remember(patch: object): void {
  try {
    window.localStorage.setItem(
      STORE_KEY,
      JSON.stringify({ ...recall(), ...patch })
    );
  } catch {
    // Not remembered; the panel still works.
  }
}

const app = document.getElementById('app');
if (app) {
  const shell = createShell(app);
  let entries: ContextEntry[] = [];
  const versionMode = params.get('versions') === 'every' ? 'every' : 'grouped';
  // Cells are named by their place and first line, from the state on show when it has them.
  const nameCell = (cellId: string, entryIndex?: number | null): string => {
    const result = session.view().shown?.result;
    const cells = result?.status === 'ok' ? result.state.notebook.cells : [];
    const at = cells.findIndex(c => c.cellId === cellId);
    return at >= 0
      ? cellName(cells[at].code, at)
      : cellName(undefined, entryIndex);
  };
  const changesList = createChanges(
    shell.changes,
    session,
    versionMode,
    nameCell
  );
  // The view: changes is the only one so far; anything else, a remembered `list` included, falls back to it.
  const isView = (v: unknown): v is PanelView => v === 'changes';
  const wantView = params.get('view') ?? recall().view;
  const showView = (view: PanelView) => {
    shell.setView(view);
    changesList.refresh();
  };
  shell.onViewChange(view => {
    remember({ view });
    showView(view);
  });
  shell.onCollapseChange(collapsed => {
    remember({ collapsed });
    shell.setCollapsed(collapsed);
  });
  shell.setCollapsed(
    params.has('panel')
      ? params.get('panel') === 'collapsed'
      : !!recall().collapsed
  );
  showView(isView(wantView) ? wantView : 'changes');
  const ghost = createGhost(
    shell.ghost,
    session,
    channel?.live,
    createOutputRenderer({
      fetchBlob: contentHash => client.blob({ explorationId, contentHash })
    }),
    { cache: createBaseCache(client, explorationId), mode: versionMode }
  );
  // The diff is against now unless the page says otherwise (`diff=previous|none`).
  const wantDiff = params.get('diff');
  session.focus.showDiff(
    wantDiff === 'previous' || wantDiff === 'none' ? wantDiff : 'now'
  );
  shell.onDiffChange(choice => session.focus.showDiff(choice));
  const showDiffSwitch = () =>
    shell.setDiff(
      (['now', 'previous'] as const).find(
        c => c === session.focus.get().diffBase
      ) ?? 'none',
      session.view().mode === 'lookingBack'
    );
  session.focus.subscribe(showDiffSwitch);
  session.subscribe(showDiffSwitch);
  showDiffSwitch();
  let asked = 0;

  // `disconnected` wins over `catching up`, which wins over `ghost view` and `live`.
  const statusOf = (view: ReturnType<typeof session.view>): ShellStatus =>
    session.connection() === 'reconnecting'
      ? 'disconnected'
      : view.freshness === 'catchingUp'
        ? 'catchingUp'
        : view.mode === 'lookingBack'
          ? 'ghost'
          : 'live';
  let namedFrom: unknown;
  const renderView = () => {
    const view = session.view();
    const result = view.shown?.result;
    renderConversation(
      shell.conversation,
      result?.status === 'ok' ? result.state.chat : undefined
    );
    if (result !== namedFrom) {
      namedFrom = result;
      changesList.setEntries(entries);
    }
    const from = view.lookingBackFromTip;
    const tip = session.activeTip();
    shell.setStatus(
      statusOf(view),
      view.mode === 'lookingBack' && from !== undefined && tip !== undefined
        ? Math.max(0, tip - from)
        : 0
    );
    if (result && result.status === 'ok') {
      shell.setNotebook(result.state.notebook.path);
    }
  };
  session.subscribe(renderView);
  renderView();

  // Details of the selected moment: its index entry and the state at its tick.
  const showSelected = () => {
    const selected = session.focus.get().selected;
    const entry = selected
      ? entries.find(e => e.tick === selected.firstTick)
      : undefined;
    const mine = ++asked;
    renderContext(shell.context, entry ?? null);
    if (!entry) {
      return;
    }
    const branchId = session.view().branchId;
    // The event's link to a chat turn (S7 writes them); none today.
    const turn = client
      .eventsBetween({
        explorationId,
        branchId,
        fromTick: entry.tick,
        toTick: entry.tick
      })
      .then(r => r.events[0]?.links?.chatTurnId ?? null)
      .catch(() => null);
    Promise.all([
      client.state({ explorationId, branchId, tick: entry.tick }),
      turn
    ])
      .then(([result, chatTurnId]) => {
        if (mine === asked && result.status === 'ok') {
          renderContext(shell.context, entry, result.state, chatTurnId);
        }
      })
      .catch(() => undefined);
  };
  session.focus.subscribe(showSelected);
  showSelected();

  let loadedBranch = '';
  let syncing: Promise<void> | undefined;
  let syncAgain = false;
  const showEntries = () => {
    changesList.setEntries(entries);
    ghost.setEntries(entries);
  };
  void syncTimeline().then(() => {
    showEntries();
    const select = Number(params.get('select'));
    const entry = entries.find(e => e.tick === select);
    if (entry) {
      session.focus.select({
        firstTick: entry.tick,
        lastTick: entry.tick,
        kind: entry.kind
      });
    }
    const tick = Number(params.get('tick'));
    if (tick > 0) {
      session.focus.goto(session.view().branchId, tick);
    }
    // From here on, the timeline follows the active branch's tip.
    session.subscribe(() => {
      void syncTimeline().then(showEntries);
    });
  });

  /** Fetches the timeline index of `branchId` from `fromTick`, page by page. */
  async function fetchEntries(
    branchId: string,
    fromTick?: number
  ): Promise<ContextEntry[]> {
    const got: ContextEntry[] = [];
    for (;;) {
      const page = await client.timeline({
        explorationId,
        branchId,
        ...(fromTick === undefined ? {} : { fromTick })
      });
      for (let i = 0; i < page.ticks.length; i++) {
        got.push(entryAt(page, i));
      }
      if (!page.truncated || page.nextTick === undefined) {
        return got;
      }
      fromTick = page.nextTick;
    }
  }

  /** Brings the entries up to the active tip: only the new ones, or all of them for another branch or after a gap. At most one fetch runs; a tip that moves meanwhile is fetched once more after it. */
  function syncTimeline(): Promise<void> {
    if (syncing) {
      syncAgain = true;
      return syncing;
    }
    syncing = (async () => {
      try {
        do {
          syncAgain = false;
          await syncOnce();
        } while (syncAgain);
      } finally {
        syncing = undefined;
      }
    })();
    return syncing;
  }
  async function syncOnce(): Promise<void> {
    const branchId = session.activeBranchId() || (await session.branch());
    const last = entries.length ? entries[entries.length - 1].tick : undefined;
    try {
      if (branchId === loadedBranch && last !== undefined) {
        const tip = session.activeTip();
        if (tip === undefined || tip <= last) {
          return;
        }
        const next = appendEntries(
          entries,
          await fetchEntries(branchId, last + 1)
        );
        if (!next.gap) {
          entries = next.entries;
          return;
        }
      }
      entries = await fetchEntries(branchId);
      loadedBranch = branchId;
    } catch {
      // The panel keeps what it has; the page keeps working without a timeline.
    }
  }
}
session.start();
