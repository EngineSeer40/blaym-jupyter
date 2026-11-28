/**
 * The page layout: a header (notebook name and state pill) and three regions,
 * the side panel, the ghost book (with the conversation region under it) and the context panel. The card fills the
 * panel's height and the regions scroll inside it. Wide puts the three side by
 * side, narrow stacks them (see `viewer.css`).
 * Every part reads only its inputs and the theme tokens.
 */
import { renderIcon } from './icons';

export type ShellStatus = 'live' | 'ghost' | 'catchingUp' | 'disconnected';

export type DiffChoice = 'now' | 'previous' | 'none';

/** The side panel's views: the branch tree and the change rows, and the search hits while a query is active (no tab). */
export type PanelView = 'tree' | 'changes' | 'search';

export interface SwitchLine {
  busy?: boolean;
  problem?: { text: string; onDismiss(): void };
  back?: { label: string; note: string; onClick(): void };
}

export interface Shell {
  /** Shows the diff switch's choice; `enabled` is false while following now. */
  setDiff(choice: DiffChoice, enabled: boolean): void;
  onDiffChange(fn: (choice: DiffChoice) => void): void;
  setNotebook(path: string): void;
  /** `newCount`: changes recorded since the user started looking back; shown after the ghost pill. */
  setStatus(status: ShellStatus, newCount?: number): void;
  /** The line shown when another tab or editor holds recording (`null` hides it); `onRecordHere` hears its button. */
  setRecorder(line: { text: string; tooltip: string } | null): void;
  onRecordHere(fn: () => void): void;
  /** The switch line: a calm "switching…" while it runs, a problem to dismiss, the return point. All absent hides it. */
  setSwitch(line: SwitchLine | null): void;
  /** Shows one view of the side panel. */
  setView(view: PanelView): void;
  onViewChange(fn: (view: PanelView) => void): void;
  /** The header search field: `onSearch` hears every edit; `setSearch('')` clears it. */
  onSearch(fn: (text: string) => void): void;
  setSearch(text: string): void;
  setCollapsed(collapsed: boolean): void;
  onCollapseChange(fn: (collapsed: boolean) => void): void;
  /** The side panel and the host of its changes view. */
  readonly panel: HTMLElement;
  readonly changes: HTMLElement;
  /** The host of the tree view. */
  readonly tree: HTMLElement;
  /** The host of the search view. */
  readonly search: HTMLElement;
  readonly ghost: HTMLElement;
  /** The conversation region under the ghost book: a collapsible `<details>` body. */
  readonly conversation: HTMLElement;
  readonly context: HTMLElement;
}

const PILL_TEXT: Record<ShellStatus, string> = {
  live: 'live',
  ghost: 'ghost view · read-only',
  catchingUp: 'catching up…',
  disconnected: 'disconnected'
};

function div(className: string, text?: string): HTMLElement {
  const e = document.createElement('div');
  e.className = className;
  if (text !== undefined) {
    e.textContent = text;
  }
  return e;
}

const DIFF_CHOICES: readonly [DiffChoice, string][] = [
  ['now', 'now'],
  ['previous', 'previous'],
  ['none', 'off']
];

const PANEL_TABS: readonly [PanelView, string][] = [
  ['tree', 'tree'],
  ['changes', 'changes']
];

const SVG_NS = 'http://www.w3.org/2000/svg';

/** The notebook glyph of the header: spine and pages, outline only. */
function notebookIcon(): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('class', 'bv-notebook-icon');
  svg.setAttribute('aria-hidden', 'true');
  const d =
    'M4 1.5h8a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1zM6 1.5v13M8 5h3M8 8h3';
  const p = document.createElementNS(SVG_NS, 'path');
  p.setAttribute('d', d);
  svg.append(p);
  return svg;
}

/** A calm banner: an icon and one line of text, for restarts, gaps and outside changes. */
export function edgeBanner(kind: string, text: string): HTMLElement {
  const banner = div('bv-banner');
  banner.setAttribute('role', 'status');
  banner.append(renderIcon(kind, null, null, 18), div('bv-banner-text', text));
  return banner;
}

export function createShell(root: HTMLElement): Shell {
  root.replaceChildren();
  const card = div('bv-card');
  const header = div('bv-header');
  const name = div('bv-notebook-name');
  const nameText = document.createElement('span');
  name.append(notebookIcon(), nameText);
  const pill = div('bv-pill');
  const recorder = div('bv-recorder');
  recorder.hidden = true;
  const recorderText = document.createElement('span');
  const recordHere = document.createElement('button');
  recordHere.type = 'button';
  recordHere.className = 'bv-record-here';
  recordHere.textContent = 'record here';
  let recordClicked: () => void = () => undefined;
  recordHere.addEventListener('click', () => recordClicked());
  recorder.append(recorderText, recordHere);
  const switchLine = div('bv-switch');
  switchLine.hidden = true;
  const diffSwitch = div('bv-diff-switch');
  diffSwitch.setAttribute('role', 'group');
  diffSwitch.setAttribute('aria-label', 'diff against');
  diffSwitch.append(div('bv-diff-label', 'diff:'));
  const buttons = new Map<DiffChoice, HTMLButtonElement>();
  let changed: (choice: DiffChoice) => void = () => undefined;
  for (const [choice, text] of DIFF_CHOICES) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'bv-diff-choice';
    b.textContent = text;
    b.addEventListener('click', () => changed(choice));
    buttons.set(choice, b);
    diffSwitch.append(b);
  }
  const find = document.createElement('input');
  find.type = 'search';
  find.className = 'bv-search';
  find.placeholder = 'search history';
  find.setAttribute('aria-label', 'search history');
  let searched: (text: string) => void = () => undefined;
  find.addEventListener('input', () => searched(find.value));
  find.addEventListener('keydown', ev => {
    if (ev.key === 'Escape' && find.value !== '') {
      ev.stopPropagation();
      find.value = '';
      searched('');
    }
  });
  header.append(name, find, diffSwitch, recorder, pill);
  const switchBar = div('bv-switch-bar');
  switchBar.append(switchLine);

  const body = div('bv-body');
  const panel = div('bv-versions');
  const tabs = div('bv-tabs');
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-label', 'side panel view');
  const tabButtons = new Map<PanelView, HTMLButtonElement>();
  let viewChanged: (view: PanelView) => void = () => undefined;
  let collapseChanged: (collapsed: boolean) => void = () => undefined;
  for (const [view, text] of PANEL_TABS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'bv-tab';
    b.textContent = text;
    b.setAttribute('role', 'tab');
    b.addEventListener('click', () => viewChanged(view));
    tabButtons.set(view, b);
    tabs.append(b);
  }
  const collapse = document.createElement('button');
  collapse.type = 'button';
  collapse.className = 'bv-collapse';
  collapse.addEventListener('click', () =>
    collapseChanged(!panel.classList.contains('bv-versions-collapsed'))
  );
  tabs.append(collapse);
  const changes = div('bv-pane');
  const tree = div('bv-pane');
  const search = div('bv-pane');
  panel.append(tabs, changes, tree, search);
  const main = div('bv-main');
  const ghost = div('bv-ghost');
  const details = document.createElement('details');
  details.className = 'bv-conversation';
  details.open = true;
  const summary = document.createElement('summary');
  summary.className = 'bv-conversation-head';
  summary.textContent = 'conversation';
  const conversation = div('bv-conversation-body');
  details.append(summary, conversation);
  main.append(ghost, details);
  const context = div('bv-context');
  body.append(panel, main, context);

  card.append(header, switchBar, body);
  const wrap = div('bv-shell');
  wrap.append(card);
  root.append(wrap);

  const shell: Shell = {
    setDiff(choice, enabled) {
      for (const [c, b] of buttons) {
        b.setAttribute('aria-pressed', String(c === choice));
        b.disabled = !enabled;
      }
      diffSwitch.classList.toggle('bv-diff-disabled', !enabled);
      diffSwitch.title = enabled
        ? 'Mark what differs from this version'
        : 'Following now: there is nothing to compare. Look back at an earlier version to see changes.';
    },
    onDiffChange(fn) {
      changed = fn;
    },
    setNotebook(path) {
      nameText.textContent = path;
    },
    setStatus(status, newCount = 0) {
      pill.textContent =
        status === 'ghost' && newCount > 0
          ? `${PILL_TEXT[status]} · ${newCount} new`
          : PILL_TEXT[status];
      pill.setAttribute('data-status', status);
    },
    setRecorder(line) {
      recorder.hidden = line === null;
      recorderText.textContent = line?.text ?? '';
      recorder.title = line?.tooltip ?? '';
    },
    onRecordHere(fn) {
      recordClicked = fn;
    },
    setSwitch(line) {
      switchLine.replaceChildren();
      switchLine.hidden = !line || (!line.busy && !line.problem && !line.back);
      switchBar.hidden = switchLine.hidden;
      if (!line) {
        return;
      }
      const add = (text: string, className = 'bv-muted') =>
        switchLine.append(div(className, text));
      const act = (text: string, onClick: () => void, title: string) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'bv-record-here bv-switch-btn';
        b.textContent = text;
        b.title = title;
        b.addEventListener('click', onClick);
        switchLine.append(b);
      };
      if (line.busy) {
        switchLine.setAttribute('role', 'status');
        add('switching…');
        return;
      }
      if (line.problem) {
        switchLine.setAttribute('role', 'status');
        add(line.problem.text);
        act('dismiss', line.problem.onDismiss, 'Hide this message');
        return;
      }
      if (line.back) {
        add(line.back.note);
        act(line.back.label, line.back.onClick, 'Switch the notebook back');
      }
    },
    setView(view) {
      for (const [v, b] of tabButtons) {
        b.setAttribute('aria-selected', String(v === view));
      }
      changes.hidden = view !== 'changes';
      tree.hidden = view !== 'tree';
      search.hidden = view !== 'search';
      panel.classList.toggle('bv-versions-tree', view === 'tree');
    },
    onSearch(fn) {
      searched = fn;
    },
    setSearch(text) {
      find.value = text;
    },
    onViewChange(fn) {
      viewChanged = fn;
    },
    setCollapsed(collapsed) {
      panel.classList.toggle('bv-versions-collapsed', collapsed);
      collapse.textContent = collapsed ? '\u00bb' : '\u00ab';
      collapse.title = collapsed
        ? 'Show the side panel'
        : 'Hide the side panel';
      collapse.setAttribute('aria-expanded', String(!collapsed));
    },
    onCollapseChange(fn) {
      collapseChanged = fn;
    },
    panel,
    changes,
    tree,
    search,
    ghost,
    conversation,
    context
  };
  shell.setView('changes');
  shell.setCollapsed(false);
  shell.setStatus('live');
  return shell;
}
