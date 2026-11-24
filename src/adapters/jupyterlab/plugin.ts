import type {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { Notification } from '@jupyterlab/apputils';
import { INotebookTracker } from '@jupyterlab/notebook';
import { ISettingRegistry } from '@jupyterlab/settingregistry';
import { IStatusBar } from '@jupyterlab/statusbar';
import type { ISignal } from '@lumino/signaling';
import { Widget } from '@lumino/widgets';
import type { MetadataPolicy } from '../../recording/metadata';
import type { Clock, Ids, Scheduler } from '../../recording/types';
import { WebLocksElection } from './election';
import policy from './metadata-policy.json';
import { NotebookObserver } from './observer';
import type { ObserverDeps, PanelLike } from './observer';
import { LiveSettings } from './settings';
import type { SettingsLike } from './settings';
import { createSignals } from './signals';
import { NotebookPresenter, toggleTarget } from './status';
import type { Described, Notifier } from './status';
import { ServerTransport } from './transport';

const TOGGLE = 'blaym-jupyter:toggle-recording';

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

/** The observers of the open notebooks (Task 16 reads them for the status and the toggle). */
export const observers = new Map<PanelLike, NotebookObserver>();

export const plugin: JupyterFrontEndPlugin<void> = {
  id: 'blaym-jupyter:plugin',
  description: 'History, replay and branching for Jupyter notebooks',
  autoStart: true,
  requires: [INotebookTracker],
  optional: [ISettingRegistry, IStatusBar],
  activate: async (
    app: JupyterFrontEnd,
    tracker: INotebookTracker,
    registry: ISettingRegistry | null,
    statusBar: IStatusBar | null
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
      signals: createSignals()
    };
    const watch = (panel: PanelLike & { disposed?: unknown }): void => {
      const button = new StateWidget('button', 'blaym-toggle');
      const presenter = new NotebookPresenter(notifier, d => {
        button.render(d);
        if (current() === panel) {
          statusItem.render(d);
        }
      });
      presenters.set(panel, presenter);
      const observer = new NotebookObserver(panel, {
        ...deps,
        election: new WebLocksElection(),
        onState: s => presenter.onState(s),
        onStatus: s => presenter.onStatus(s)
      });
      observers.set(panel, observer);
      button.node.addEventListener('click', () => {
        void app.commands.execute(TOGGLE);
      });
      (
        panel as unknown as {
          toolbar?: { insertItem(i: number, n: string, w: Widget): boolean };
        }
      ).toolbar?.insertItem(11, 'blaym-recording', button);
      panel.context.sessionContext.statusChanged?.connect((_, status) =>
        presenter.onKernelStatus(String(status))
      );
      panel.context.disposed.connect(() => {
        observers.delete(panel);
        presenters.delete(panel);
      });
      void observer.start();
    };
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
