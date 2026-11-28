import './viewer.css';
import { createClient } from './client';
import type { Backend } from './client';
import {
  createHostChannel,
  requestSwitch,
  watchRecorder,
  windowPort
} from './host';
import { createBaseCache } from './diffbase';
import { createGhost } from './ghost';
import { createOutputRenderer, createQuickOutputRenderer } from './outputs';
import { createFrames, createSizeMemory } from './frames';
import { renderContext } from './context';
import { createTimelines } from './timelines';
import { branchLabel } from './treelayout';
import { createSession } from './session';
import { createChanges } from './changes';
import { createTree } from './tree';
import { createSearch } from './search';
import { cellName } from './cellname';
import { renderConversation } from './conversation';
import { createShell } from './shell';
import type { PanelView, ShellStatus } from './shell';
import { startTheme } from './theme';

/** The address fragment: `#explorationId=..&mode=direct|host&base=..&branch=..&orientation=..`,
 * and optionally `versions=every|sessions` (the version list's mode), `view=changes` and `panel=collapsed|open` (the side panel), `search=` (open with a query typed), `tick=` (open looking back at that tick) and `select=` (select the timeline entry at that tick). */
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
  activeBranchId: params.get('branch') ?? '',
  switchHost: requestSwitch
});

/** The remembered side panel choices; the webview may block local storage, so every use is guarded. */
const STORE_KEY = 'blaym-panel';
function recall(): { view?: string; collapsed?: boolean; follow?: boolean } {
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
  const timelines = createTimelines({ client, explorationId, session });
  const versionsParam = params.get('versions');
  const versionMode =
    versionsParam === 'every' || versionsParam === 'sessions'
      ? versionsParam
      : 'grouped';
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
  // `frames=moving` (harness) holds the moving state; `pace` and `lag` tune the typing.
  const frames = createFrames({ hold: params.get('frames') === 'moving' });
  const sizes = createSizeMemory();
  const scrollParam = params.get('tscroll');
  const treeView = createTree(shell.tree, session, timelines, nameCell, {
    zoom: Number(params.get('zoom')) || undefined,
    scroll:
      scrollParam === 'oldest'
        ? 'oldest'
        : scrollParam
          ? Number(scrollParam)
          : undefined,
    follow: params.get('follow') === '0' ? false : recall().follow !== false,
    onFollowChange: follow => remember({ follow }),
    frames
  });
  // The view: anything unknown, a remembered `list` included, falls back to changes.
  const isView = (v: unknown): v is PanelView =>
    v === 'changes' || v === 'tree';
  // The view shown before a search began, and the one the panel returns to.
  let before: PanelView = 'changes';
  let searching = false;
  const wantView = params.get('view') ?? recall().view;
  const showView = (view: PanelView) => {
    if (view !== 'search') {
      before = view;
    }
    shell.setView(view);
    changesList.refresh();
    treeView.refresh();
  };
  shell.onViewChange(view => {
    remember({ view });
    showView(view);
  });
  const search = createSearch(shell.search, {
    client,
    explorationId,
    session,
    timelines,
    scheduler
  });
  // A query shows the hits; clearing it (Escape too) returns to the earlier view.
  const onSearch = (text: string) => {
    search.setQuery(text);
    const active = text.trim() !== '';
    if (active !== searching) {
      searching = active;
      showView(active ? 'search' : before);
    }
  };
  shell.onSearch(onSearch);
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
  // `search=` opens with that query typed (the harness).
  const wantSearch = params.get('search');
  if (wantSearch) {
    shell.setSearch(wantSearch);
    onSearch(wantSearch);
  }
  const ghost = createGhost(
    shell.ghost,
    session,
    channel?.live,
    createOutputRenderer({
      fetchBlob: contentHash => client.blob({ explorationId, contentHash }),
      sizes
    }),
    { cache: createBaseCache(client, explorationId), mode: versionMode },
    {
      frames,
      output: createQuickOutputRenderer(sizes),
      pace: Number(params.get('pace')) || undefined,
      lagMs: Number(params.get('lag')) || undefined
    }
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
      changesList.setEntries(timelines.viewed());
    }
    // Only the active branch's own steps count; global ticks include other branches.
    const from = view.lookingBackFromTip;
    shell.setStatus(
      statusOf(view),
      view.mode === 'lookingBack' && from !== undefined
        ? timelines.own(session.activeBranchId()).filter(e => e.tick > from)
            .length
        : 0
    );
    if (result && result.status === 'ok') {
      shell.setNotebook(result.state.notebook.path);
    }
  };
  // The switch line: switching, a problem to dismiss, or the way back to the branch left.
  const renderSwitch = () => {
    const problem = session.problem();
    const back = session.previousBranchId();
    const meta = timelines.branches().find(b => b.branchId === back);
    shell.setSwitch({
      busy: session.switching(),
      problem: problem
        ? { text: problem, onDismiss: () => session.dismissProblem() }
        : undefined,
      back:
        back && meta
          ? {
              label: `back to ${branchLabel(meta)}`,
              note: 'Variables are not restored: the kernel was restarted.',
              onClick: () => session.switchTo(back)
            }
          : undefined
    });
  };
  session.subscribe(renderView);
  session.subscribe(renderSwitch);
  renderView();
  renderSwitch();
  shell.onRecordHere(watchRecorder(line => shell.setRecorder(line)));

  // Details of the selected moment: its index entry and the state at its tick.
  const showSelected = () => {
    const selected = session.focus.get().selected;
    const entry = selected
      ? timelines.viewed().find(e => e.tick === selected.firstTick)
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

  const showEntries = () => {
    const entries = timelines.viewed();
    changesList.setEntries(entries);
    ghost.setEntries(entries);
    treeView.update();
    renderView();
  };
  void timelines.sync().then(() => {
    showEntries();
    const select = Number(params.get('select'));
    const entry = timelines.viewed().find(e => e.tick === select);
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
    // From here on, the timelines follow the active branch's tip.
    session.subscribe(() => {
      void timelines.sync().then(showEntries);
    });
  });
}
session.start();
