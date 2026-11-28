import type { JupyterFrontEnd } from '@jupyterlab/application';
import {
  MainAreaWidget,
  Notification,
  ToolbarButton
} from '@jupyterlab/apputils';
import type { ICommandPalette, IThemeManager } from '@jupyterlab/apputils';
import { PageConfig } from '@jupyterlab/coreutils';
import type { INotebookTracker } from '@jupyterlab/notebook';
import { Widget } from '@lumino/widgets';
import { JP_VARIABLES } from 'blaym-notebook-core';
import type { Described } from './status';
import type { SwitchResult } from './switch';

export const SHOW_HISTORY = 'blaym-jupyter:show-history';

/** The notebook panel as far as the history view needs it. */
export interface HistoryPanel extends Widget {
  title: Widget['title'];
  context: { path: string };
  content: {
    widgets: ReadonlyArray<{ model: { id: string } }>;
    activeCellIndex: number;
    scrollToItem?(index: number): unknown;
  };
}

export interface HistoryDeps {
  app: JupyterFrontEnd;
  tracker: INotebookTracker;
  palette: ICommandPalette | null;
  themeManager: IThemeManager | null;
  /** `panel` or `window` (the setting `viewerPlacement`). */
  placement: () => string;
  /** The Blaym id (the history's `explorationId`) of a notebook, once it is registered. */
  notebookId: (panel: HistoryPanel) => string | undefined;
  /** "Record here" for a notebook another tab or editor is recording. */
  recordHere: (panel: HistoryPanel) => Promise<void>;
  /** Switch the notebook at `path` to a branch (opening it first if it is closed). */
  switchBranch: (
    panel: HistoryPanel,
    path: string,
    branchId: string
  ) => Promise<SwitchResult>;
}

function viewerUrl(id: string): string {
  const base = `${PageConfig.getBaseUrl()}blaym-jupyter/api`;
  const fragment = new URLSearchParams({
    explorationId: id,
    mode: 'direct',
    base
  });
  return `${base}/viewer/index.html#${fragment.toString()}`;
}

function themeKind(themeManager: IThemeManager | null): 'light' | 'dark' {
  const theme = themeManager?.theme;
  if (themeManager && theme) {
    return themeManager.isLight(theme) ? 'light' : 'dark';
  }
  return document.body.dataset.jpThemeLight === 'false' ? 'dark' : 'light';
}

/** Moves the live notebook to a cell. The Blaym id is the model id unless the recorder had to rename it; then there is no mapping and nothing happens. */
function scrollToCell(
  app: JupyterFrontEnd,
  panel: HistoryPanel,
  cellId: string
): void {
  const index = panel.content.widgets.findIndex(w => w.model.id === cellId);
  if (index < 0) {
    return;
  }
  app.shell.activateById(panel.id);
  panel.content.activeCellIndex = index;
  panel.content.scrollToItem?.(index);
}

/** The "Blaym: show history" command, its toolbar button, and the panel or window it opens. */
export class HistoryViews {
  private readonly panels = new Map<HistoryPanel, MainAreaWidget<Widget>>();
  private readonly recorder = new Map<HistoryPanel, Described>();
  /** The viewer pages that asked about the recorder, per notebook. */
  private readonly pages = new Map<HistoryPanel, Set<Window>>();

  constructor(private readonly deps: HistoryDeps) {
    const { app, tracker, palette } = deps;
    app.commands.addCommand(SHOW_HISTORY, {
      label: 'Blaym: show history',
      isEnabled: () => !!tracker.currentWidget,
      execute: () => this.show()
    });
    palette?.addItem({ command: SHOW_HISTORY, category: 'Blaym' });
  }

  /** A toolbar button for one notebook panel, next to the recording toggle. */
  addButton(panel: HistoryPanel & { toolbar?: Widget }): void {
    const toolbar = panel.toolbar as unknown as
      { insertItem(i: number, n: string, w: Widget): boolean } | undefined;
    toolbar?.insertItem(
      12,
      'blaym-history',
      new ToolbarButton({
        label: 'History',
        tooltip: 'Blaym: show history',
        className: 'blaym-history-button',
        onClick: () => this.show(panel)
      })
    );
  }

  /** The recording state of a notebook changed: tell the viewer pages that asked. */
  recorderChanged(panel: HistoryPanel, state: Described): void {
    this.recorder.set(panel, state);
    this.pages.get(panel)?.forEach(win => this.tell(win, panel));
  }

  private tell(win: Window, panel: HistoryPanel): void {
    const d = this.recorder.get(panel);
    win.postMessage(
      {
        type: 'blaym:recorder',
        here: !d?.recordHere,
        ...(d?.recordHere
          ? { line: d.line ?? d.label, tooltip: d.tooltip }
          : {})
      },
      window.location.origin
    );
  }

  show(target?: HistoryPanel): void {
    const panel = target ?? (this.deps.tracker.currentWidget as HistoryPanel);
    const id = panel ? this.deps.notebookId(panel) : undefined;
    if (!panel || !id) {
      Notification.info(
        'Blaym has no history for this notebook yet. Turn recording on first.',
        { autoClose: 6000 }
      );
      return;
    }
    const open = this.panels.get(panel);
    if (open && !open.isDisposed) {
      this.deps.app.shell.activateById(open.id);
      return;
    }
    const url = viewerUrl(id);
    if (this.deps.placement() === 'window') {
      const win = window.open(url, '_blank');
      if (win) {
        this.listen(() => win, panel);
      }
      return;
    }
    this.panels.set(panel, this.open(panel, url));
  }

  private open(panel: HistoryPanel, url: string): MainAreaWidget<Widget> {
    const { app, themeManager } = this.deps;
    const iframe = document.createElement('iframe');
    iframe.className = 'blaym-history-frame';
    iframe.style.cssText = 'width:100%;height:100%;border:0;';
    iframe.src = url;
    const content = new Widget({ node: iframe });
    const widget = new MainAreaWidget({ content });
    widget.id = `blaym-history-${panel.id}`;
    widget.title.label = `History: ${panel.title.label}`;
    widget.title.closable = true;
    const postTheme = (): void => {
      const win = iframe.contentWindow;
      if (!win) {
        return;
      }
      const style = getComputedStyle(document.body);
      const values: Record<string, string> = {};
      for (const name of JP_VARIABLES) {
        values[name] = style.getPropertyValue(name).trim();
      }
      win.postMessage(
        { type: 'blaym:theme', kind: themeKind(themeManager), values },
        window.location.origin
      );
    };
    iframe.addEventListener('load', postTheme);
    themeManager?.themeChanged.connect(postTheme);
    const stop = this.listen(() => iframe.contentWindow, panel);
    widget.disposed.connect(() => {
      themeManager?.themeChanged.disconnect(postTheme);
      stop();
      this.panels.delete(panel);
    });
    app.shell.add(widget, 'main', { mode: 'tab-after' });
    app.shell.activateById(widget.id);
    return widget;
  }

  /** Acts on `scrollToCell` and `blaym:switch` from `source()` only; `decorateCell` is accepted and ignored. */
  private listen(source: () => Window | null, panel: HistoryPanel): () => void {
    // The panel may be closed by the time the page asks to switch: keep the path.
    const path = panel.context.path;
    const on = (ev: MessageEvent): void => {
      const win = source();
      if (!win || ev.source !== win || ev.origin !== window.location.origin) {
        return;
      }
      const m = ev.data as { type?: unknown; call?: unknown; args?: unknown };
      if (m?.type === 'blaym:recorder') {
        if (m.call === 'get') {
          const pages = this.pages.get(panel) ?? new Set<Window>();
          pages.forEach(p => p.closed && pages.delete(p));
          pages.add(win);
          this.pages.set(panel, pages);
          this.tell(win, panel);
        } else if (m.call === 'recordHere') {
          void this.deps.recordHere(panel);
        }
        return;
      }
      if (m?.type === 'blaym:switch') {
        const branchId = (m as { branchId?: unknown }).branchId;
        if (typeof branchId === 'string') {
          void this.deps
            .switchBranch(panel, path, branchId)
            .catch((e): SwitchResult => ({
              status: 'failed',
              reason: String(e)
            }))
            .then(r =>
              win.postMessage(
                { type: 'blaym:switch', branchId, ...r },
                window.location.origin
              )
            );
        }
        return;
      }
      if (m?.type !== 'blaym:live' || !Array.isArray(m.args)) {
        return;
      }
      if (m.call === 'scrollToCell' && typeof m.args[0] === 'string') {
        scrollToCell(this.deps.app, panel, m.args[0]);
      }
    };
    window.addEventListener('message', on);
    return () => window.removeEventListener('message', on);
  }
}
