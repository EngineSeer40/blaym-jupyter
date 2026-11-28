import type {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import {
  Dialog,
  ICommandPalette,
  IThemeManager,
  Notification,
  showDialog
} from '@jupyterlab/apputils';
import { INotebookTracker } from '@jupyterlab/notebook';
import { ISettingRegistry } from '@jupyterlab/settingregistry';
import { IStatusBar } from '@jupyterlab/statusbar';
import type { ISignal } from '@lumino/signaling';
import { Widget } from '@lumino/widgets';
import type { MetadataPolicy } from 'blaym-notebook-core';
import type { Clock, Ids, Scheduler } from 'blaym-notebook-core';
import { WebLocksElection } from './election';
import { metadataPolicy as policy } from 'blaym-notebook-core';
import { NotebookObserver } from './observer';
import type { ObserverDeps, PanelLike } from './observer';
import { HistoryViews } from './history';
import type { HistoryPanel } from './history';
import { LiveSettings } from './settings';
import type { SettingsLike } from './settings';
import { createSignals } from './signals';
import { NotebookPresenter, toggleTarget } from './status';
import type { Described, Notifier } from './status';
import { ServerTransport } from './transport';
import { BranchSwitch, ServiceTarget } from './switch';
import type { SwitchResult, SwitchShared } from './switch';

const TOGGLE = 'blaym-jupyter:toggle-recording';
const RECORD_HERE = 'blaym-jupyter:record-here';

/** A small text widget that shows a state (status bar item and toolbar toggle). */
class StateWidget extends Widget {
  constructor(tag: 'span' | 'button', cls: string) {
    super({ node: document.createElement(tag) });
    this.addClass(cls);
    this.node.dataset.blaymState = 'starting';
    this.node.textContent = 'Blaym: starting';
  }

  render(d?: Described): void {
    if (!d) {
      return;
    }
    this.node.dataset.blaymState = d.key;
    this.node.textContent = d.label;
    this.node.title = d.tooltip;
    this.node.style.cursor = d.recordHere ? 'pointer' : '';
  }
}

const notifier: Notifier = {
  notify: (severity, text) => {
    Notification[severity](text, { autoClose: 6000 });
  }
};

export async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

const clock: Clock = { nowMs: () => Date.now() };
const scheduler: Scheduler = {
  setTimeout: (fn, ms) => {
    const t = window.setTimeout(fn, ms);
    return () => window.clearTimeout(t);
  }
};
const ids: Ids = { uuid: () => crypto.randomUUID() };

/** The observers of the open notebooks (read for the status and the toggle). */
export const observers = new Map<PanelLike, NotebookObserver>();

export const plugin: JupyterFrontEndPlugin<void> = {
  id: 'blaym-jupyter:plugin',
  description: 'History, replay and branching for Jupyter notebooks',
  autoStart: true,
  requires: [INotebookTracker],
  optional: [ISettingRegistry, IStatusBar, ICommandPalette, IThemeManager],
  activate: async (
    app: JupyterFrontEnd,
    tracker: INotebookTracker,
    registry: ISettingRegistry | null,
    statusBar: IStatusBar | null,
    palette: ICommandPalette | null,
    themeManager: IThemeManager | null
  ) => {
    let source: SettingsLike | undefined;
    try {
      source = (await registry?.load(plugin.id)) ?? undefined;
    } catch {
      source = undefined;
    }
    const live = new LiveSettings(source);
    const statusItem = new StateWidget('span', 'blaym-status');
    const presenters = new Map<PanelLike, NotebookPresenter>();
    const notebookIds = new Map<PanelLike, string>();
    const current = (): PanelLike | null =>
      tracker.currentWidget as unknown as PanelLike | null;
    statusBar?.registerStatusItem('blaym-jupyter:status', {
      item: statusItem,
      align: 'left',
      rank: 900,
      isActive: () => !!tracker.currentWidget,
      activeStateChanged: tracker.currentChanged as unknown as ISignal<
        unknown,
        void
      >
    });
    tracker.currentChanged.connect(() => {
      const p = current();
      statusItem.render(p ? presenters.get(p)?.described : undefined);
    });
    const recordHere = async (panel?: PanelLike | null): Promise<void> => {
      const o = panel ? observers.get(panel) : undefined;
      await o?.recordHere();
    };
    app.commands.addCommand(RECORD_HERE, {
      label: 'Blaym: record here',
      isEnabled: () => {
        const p = current();
        return !!p && !!presenters.get(p)?.described?.recordHere;
      },
      execute: () => recordHere(current())
    });
    statusItem.node.addEventListener('click', () => {
      void app.commands.execute(RECORD_HERE);
    });
    app.commands.addCommand(TOGGLE, {
      label: 'Toggle Blaym recording for this notebook',
      isEnabled: () => {
        const p = current();
        const o = p ? observers.get(p) : undefined;
        return !!o && toggleTarget(o.currentState) !== undefined;
      },
      execute: async () => {
        const p = current();
        const o = p ? observers.get(p) : undefined;
        const target = o ? toggleTarget(o.currentState) : undefined;
        if (o && target !== undefined) {
          await o.setRecording(target);
        }
      }
    });
    /** Notebooks in the middle of a branch switch (the recorder reads this at event time). */
    const switching = new Set<PanelLike>();
    const pause = (ms: number): Promise<void> =>
      new Promise(resolve => window.setTimeout(resolve, ms));
    type SwitchPanel = PanelLike & {
      isDisposed: boolean;
      isVisible: boolean;
      id: string;
      content: { activate(): void };
      context: PanelLike['context'] & {
        save(): Promise<void>;
        model: { dirty: boolean };
      };
    };
    /** The open notebook at `path`, opened through the document manager if it is closed. */
    const notebookAt = async (
      panel: HistoryPanel,
      path: string
    ): Promise<SwitchPanel> => {
      let found = panel.isDisposed
        ? tracker.find(w => w.context.path === path)
        : panel;
      if (!found) {
        found = (await app.commands.execute('docmanager:open', {
          path,
          factory: 'Notebook'
        })) as typeof found;
      }
      const p = found as unknown as SwitchPanel;
      await p.context.ready;
      // the recorder registers the notebook once it is open: wait for its id
      for (let i = 0; i < 100 && !notebookIds.has(p); i++) {
        await pause(50);
      }
      return p;
    };
    const switchBranch = async (
      panel: HistoryPanel,
      path: string,
      branchId: string
    ): Promise<SwitchResult> => {
      let p: SwitchPanel;
      try {
        p = await notebookAt(panel, path);
      } catch (e) {
        return {
          status: 'failed',
          reason: `Could not open the notebook (${e})`
        };
      }
      const id = notebookIds.get(p);
      if (!id) {
        return {
          status: 'failed',
          reason: 'Blaym has no history for this notebook yet'
        };
      }
      const service = new ServiceTarget(id, app.serviceManager.serverSettings);
      const ctx = p.context;
      const observer = observers.get(p);
      return new BranchSwitch({
        key: path,
        busy: () =>
          (
            ctx.sessionContext as unknown as {
              session?: { kernel?: { status?: string } | null } | null;
            }
          ).session?.kernel?.status === 'busy',
        shared: () => ctx.model.sharedModel as unknown as SwitchShared,
        flushTyping: () => observer?.flushTyping(),
        dirty: () => ctx.model.dirty,
        confirm: async () =>
          (
            await showDialog({
              title: 'Unsaved changes',
              body: 'This notebook has changes that are not saved. Save them and switch, or cancel and stay as you are.',
              buttons: [
                Dialog.cancelButton(),
                Dialog.okButton({ label: 'Save and switch' })
              ]
            })
          ).button.accept,
        save: () => ctx.save(),
        fetchTarget: b => service.fetchTarget(b),
        setActive: b => service.setActive(b),
        setSwitching: on => (on ? switching.add(p) : switching.delete(p)),
        wait: pause,
        adoptBaseline: cells => observer?.adoptBaseline(cells),
        restartKernel: async () => {
          const sc = ctx.sessionContext as unknown as {
            session?: { kernel?: unknown } | null;
            restartKernel(): Promise<void>;
          };
          if (!sc.session?.kernel) {
            return false;
          }
          await sc.restartKernel();
          return true;
        },
        refocus: () => {
          if (p.isVisible) {
            p.content.activate();
          }
        },
        notify: (severity, text) =>
          Notification[severity](text, { autoClose: 8000 })
      }).run(branchId);
    };
    const history = new HistoryViews({
      app,
      tracker,
      palette,
      themeManager,
      placement: () =>
        source?.composite.viewerPlacement === 'window' ? 'window' : 'panel',
      notebookId: p => notebookIds.get(p as unknown as PanelLike),
      recordHere: p => recordHere(p as unknown as PanelLike),
      switchBranch
    });
    const transport = new ServerTransport(app.serviceManager.serverSettings);
    const deps: ObserverDeps = {
      transport,
      clock,
      scheduler,
      ids,
      election: new WebLocksElection(),
      policy: policy as MetadataPolicy,
      sha256Hex,
      settings: live.observer,
      signals: createSignals(app)
    };
    const watch = (panel: PanelLike & { disposed?: unknown }): void => {
      const button = new StateWidget('button', 'blaym-toggle');
      const presenter = new NotebookPresenter(notifier, d => {
        button.render(d);
        history.recorderChanged(panel as unknown as HistoryPanel, d);
        if (current() === panel) {
          statusItem.render(d);
        }
      });
      presenters.set(panel, presenter);
      const observer = new NotebookObserver(panel, {
        ...deps,
        election: new WebLocksElection(),
        switching: () => switching.has(panel),
        onState: s => presenter.onState(s),
        onStatus: s => {
          notebookIds.set(panel, s.notebookId);
          presenter.onStatus(s);
        }
      });
      observers.set(panel, observer);
      button.node.addEventListener('click', () => {
        void (presenter.described?.recordHere
          ? recordHere(panel)
          : app.commands.execute(TOGGLE));
      });
      (
        panel as unknown as {
          toolbar?: { insertItem(i: number, n: string, w: Widget): boolean };
        }
      ).toolbar?.insertItem(11, 'blaym-recording', button);
      history.addButton(panel as unknown as HistoryPanel);
      panel.context.sessionContext.statusChanged?.connect((_, status) =>
        presenter.onKernelStatus(String(status))
      );
      panel.context.disposed.connect(() => {
        observers.delete(panel);
        presenters.delete(panel);
        notebookIds.delete(panel);
      });
      void observer.start();
    };
    // The notebook commands act on the current notebook.
    app.commands.commandExecuted.connect((_, args) => {
      const p = current();
      if (p) {
        observers.get(p)?.commandExecuted(args.id);
      }
    });
    tracker.forEach(p => watch(p as unknown as PanelLike));
    tracker.widgetAdded.connect((_, p) => watch(p as unknown as PanelLike));
    const forAll = (f: (o: NotebookObserver) => void): void =>
      observers.forEach(f);
    window.addEventListener('pagehide', () => forAll(o => o.hide('pagehide')));
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') {
        forAll(o => o.hide('hidden'));
      }
    });
  }
};
