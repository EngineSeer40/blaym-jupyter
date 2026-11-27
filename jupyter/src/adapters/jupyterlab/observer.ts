import type {
  CellType,
  NotebookRegistration,
  NotebookStatus
} from 'blaym-notebook-types';
import { notebookFingerprint, RecorderSession } from 'blaym-notebook-core';
import type {
  TipCell,
  MetadataPolicy,
  OutboxReport,
  Transport,
  AuthorSignals,
  CellView,
  Clock,
  Ids,
  RecorderSettings,
  RecorderState,
  Scheduler,
  TabElection
} from 'blaym-notebook-core';

/* ------------------------------------------------------------------ */
/* The parts of JupyterLab the observer touches (structural, so a fake */
/* can stand in for them in tests).                                    */
/* ------------------------------------------------------------------ */

export interface SignalLike<A = unknown> {
  connect(slot: (sender: unknown, args: A) => void): unknown;
  disconnect(slot: (sender: unknown, args: A) => void): unknown;
}

export interface SharedCellLike {
  cell_type: string;
  getId(): string;
  getSource(): string;
  getMetadata(): Record<string, unknown>;
  getOutputs?(): Record<string, unknown>[];
  setId?(id: string): void;
  changed: SignalLike;
}

export interface SharedNotebookLike {
  cells: SharedCellLike[];
  nbformat: number;
  nbformat_minor: number;
  getMetadata(): Record<string, unknown>;
  transact(f: () => void): void;
  changed: SignalLike;
}

export interface KernelLike {
  name?: string;
  anyMessage: SignalLike<{
    direction: 'send' | 'recv';
    msg: {
      header: { msg_type: string; msg_id?: string };
      parent_header?: { msg_id?: string };
      metadata?: Record<string, unknown>;
    };
  }>;
}

export interface SessionContextLike {
  session?: { kernel?: KernelLike | null } | null;
  kernelChanged: SignalLike;
  connectionStatusChanged: SignalLike;
  statusChanged?: SignalLike<string>;
}

export interface ContextLike {
  path: string;
  ready: Promise<void>;
  model: {
    sharedModel: SharedNotebookLike;
    collaborative?: boolean;
  };
  saveState: SignalLike<string>;
  disposed: SignalLike;
  sessionContext: SessionContextLike;
}

export interface PanelLike {
  context: ContextLike;
  /** The notebook widget: which cells a command on the selection concerns. */
  content?: {
    widgets: ReadonlyArray<{ model: { id: string } }>;
    isSelectedOrActive(widget: unknown): boolean;
  };
}

/**
 * The user's clear commands. "Restart Kernel and Clear Outputs of All Cells"
 * (`notebook:restart-clear-output`) is not listed: after a confirmed restart it
 * executes the clear-all command itself.
 */
const CLEAR_SELECTED = 'notebook:clear-cell-output';
const CLEAR_ALL = 'notebook:clear-all-cell-outputs';

/* ------------------------------------------------------------------ */

export type ObserverState = RecorderState;

export type ObserverSettings = RecorderSettings;

export interface ObserverDeps {
  transport: Transport & { tip?(explorationId: string): Promise<TipCell[]> };
  clock: Clock;
  scheduler: Scheduler;
  ids: Ids;
  election: TabElection & {
    /** Resolves true when the lock is taken after the holder went away. */
    takeover?(notebookId: string): Promise<boolean>;
    releaseLock?(): void;
    cancelTakeover?(): void;
  };
  policy: MetadataPolicy;
  sha256Hex(text: string): Promise<string>;
  settings?: ObserverSettings;
  /** The author signals (default: none). */
  signals?: AuthorSignals;
  /** The switching flag: not yet set; read at event time. */
  switching?: () => boolean;
  /** Seams for the status and the toggle. */
  onState?: (state: ObserverState) => void;
  onReport?: (report: OutboxReport) => void;
  onStatus?: (status: NotebookStatus) => void;
}

function formatIsStable(nbformat: number, minor: number): boolean {
  return nbformat > 4 || (nbformat === 4 && minor >= 5);
}

/** Records one notebook panel: the JupyterLab side of the neutral recorder. */
export class NotebookObserver {
  readonly instanceId: string;
  private readonly session: RecorderSession;
  private shared: SharedNotebookLike;
  private cellHandlers = new Map<SharedCellLike, () => void>();
  /**
   * Model ids of the cells that have shown output since the user last cleared
   * them. A clear command is reported after the output is already gone.
   */
  private hadOutput = new Set<string>();
  private kernel?: KernelLike | null;
  private disposed = false;
  private readonly disposers: Array<() => void> = [];

  constructor(
    private readonly panel: PanelLike,
    private readonly deps: ObserverDeps
  ) {
    this.shared = panel.context.model.sharedModel;
    const stable = (): boolean => this.stable();
    this.session = new RecorderSession(
      {
        get cellIdsStable(): boolean {
          return stable();
        },
        notebookMetadata: () => this.shared.getMetadata(),
        cells: (): CellView[] =>
          this.shared.cells.map(c => ({
            id: c.getId(),
            type: c.cell_type as CellType,
            source: c.getSource(),
            metadata: c.getMetadata(),
            outputs: c.cell_type === 'code' ? (c.getOutputs?.() ?? []) : []
          }))
      },
      {
        transport: deps.transport,
        clock: deps.clock,
        scheduler: deps.scheduler,
        ids: deps.ids,
        policy: deps.policy,
        editor: 'jupyterlab',
        path: () => panel.context.path,
        ensureIds: () => this.ensureIds(),
        kernelNameFallback: () =>
          panel.context.sessionContext.session?.kernel?.name,
        collaboration: () => !!panel.context.model.collaborative,
        settings: deps.settings,
        signals: deps.signals,
        switching: deps.switching,
        onState: deps.onState,
        onReport: deps.onReport,
        onStatus: deps.onStatus
      }
    );
    this.instanceId = this.session.instanceId;
  }

  get currentState(): ObserverState {
    return this.session.state;
  }

  private stable(): boolean {
    return formatIsStable(this.shared.nbformat, this.shared.nbformat_minor);
  }

  /** After `context.ready`: register, take part in the election, start the heartbeat. */
  async start(): Promise<void> {
    const ctx = this.panel.context;
    await ctx.ready;
    if (this.disposed) {
      return;
    }
    this.shared = ctx.model.sharedModel;
    this.session.resetIdentity();
    this.ensureIds();
    this.attach();
    this.session.setFingerprint(await this.computeFingerprint());
    const status = await this.session.register();
    if (!status) {
      return;
    }
    const notebookId = status.notebookId;
    if (status.reason === 'collaboration_not_supported') {
      this.session.setState({ kind: 'inactive', reason: 'collaboration' });
      return;
    }
    if ((await this.deps.election.acquire(notebookId)) === 'inactive') {
      this.session.setState({ kind: 'inactive', reason: 'other_tab' });
      void this.waitForTakeover(notebookId);
      return;
    }
    if (this.disposed) {
      this.deps.election.releaseLock?.();
      return;
    }
    await this.session.start(notebookId);
  }

  /** A tab that lost the election takes over when the holder goes away. */
  private async waitForTakeover(notebookId: string): Promise<void> {
    const won = (await this.deps.election.takeover?.(notebookId)) ?? false;
    if (this.disposed) {
      if (won) {
        this.deps.election.releaseLock?.();
      }
      return;
    }
    if (!won) {
      return;
    }
    await this.session.start(notebookId);
  }

  /** Switch recording on or off for this notebook (the registry holds the flag). */
  setRecording(on: boolean): Promise<void> {
    return this.session.setRecording(on);
  }

  /** `visibilitychange` to hidden: send what is queued. `pagehide`: also a clean close. */
  hide(kind: 'hidden' | 'pagehide'): void {
    this.session.flushKeepalive(kind === 'pagehide');
  }

  /** The model object behind the panel was replaced (revert, reload): a resync. */
  modelReplaced(): void {
    this.shared = this.panel.context.model.sharedModel;
    this.attach();
    this.session.modelReplaced();
  }

  /** A JupyterLab command ran on this panel: a clear command is recorded. */
  commandExecuted(command: string): void {
    if (command !== CLEAR_SELECTED && command !== CLEAR_ALL) {
      return;
    }
    const content = this.panel.content;
    const ids =
      command === CLEAR_ALL
        ? this.shared.cells.map(c => c.getId())
        : (content?.widgets ?? [])
            .filter(w => content?.isSelectedOrActive(w))
            .map(w => w.model.id);
    // First send any cell change not yet seen, so history knows the cell.
    this.process();
    ids.forEach(id => {
      if (this.hadOutput.delete(id)) {
        this.session.outputsCleared(id);
      }
    });
  }

  dispose(): void {
    this.disposed = true;
    this.kernel?.anyMessage.disconnect(this.onKernelMessage);
    this.kernel = undefined;
    this.deps.election.cancelTakeover?.();
    this.sharedDisconnect?.();
    this.disposers.splice(0).forEach(d => d());
    this.cellHandlers.forEach((h, c) => c.changed.disconnect(h));
    this.cellHandlers.clear();
    this.session.close();
  }

  /* ----------------------------- wiring ----------------------------- */

  private attach(): void {
    const shared = this.shared;
    const onShared = (): void => {
      this.syncCellHandlers();
      this.process();
    };
    this.sharedDisconnect?.();
    shared.changed.connect(onShared);
    this.sharedDisconnect = () => shared.changed.disconnect(onShared);
    this.syncCellHandlers();
    const ctx = this.panel.context;
    if (!this.contextConnected) {
      this.contextConnected = true;
      const onSave = (_: unknown, state: string): void => {
        if (state === 'started') {
          this.session.flushTyping();
        }
      };
      ctx.saveState.connect(onSave);
      this.disposers.push(() => ctx.saveState.disconnect(onSave));
      const onKernel = (): void => this.subscribeKernel();
      ctx.sessionContext.kernelChanged.connect(onKernel);
      const onConnection = (): void => this.subscribeKernel();
      ctx.sessionContext.connectionStatusChanged.connect(onConnection);
      this.disposers.push(() => {
        ctx.sessionContext.kernelChanged.disconnect(onKernel);
        ctx.sessionContext.connectionStatusChanged.disconnect(onConnection);
      });
      this.subscribeKernel();
      const onDisposed = (): void => this.dispose();
      ctx.disposed.connect(onDisposed);
    }
  }
  private sharedDisconnect?: () => void;
  private contextConnected = false;

  private syncCellHandlers(): void {
    const live = new Set(this.shared.cells);
    this.cellHandlers.forEach((h, c) => {
      if (!live.has(c)) {
        c.changed.disconnect(h);
        this.cellHandlers.delete(c);
      }
    });
    live.forEach(c => {
      this.noteOutputs(c);
      if (!this.cellHandlers.has(c)) {
        const h = (): void => {
          this.noteOutputs(c);
          this.process();
        };
        c.changed.connect(h);
        this.cellHandlers.set(c, h);
      }
    });
  }

  private noteOutputs(cell: SharedCellLike): void {
    if (cell.cell_type !== 'code') {
      // History keeps no output on a cell that is not code.
      this.hadOutput.delete(cell.getId());
    } else if (
      !this.hadOutput.has(cell.getId()) &&
      (cell.getOutputs?.().length ?? 0) > 0
    ) {
      this.hadOutput.add(cell.getId());
    }
  }

  private subscribeKernel(): void {
    const kernel = this.panel.context.sessionContext.session?.kernel ?? null;
    if (kernel === this.kernel) {
      return;
    }
    const handler = this.onKernelMessage;
    this.kernel?.anyMessage.disconnect(handler);
    this.kernel = kernel;
    kernel?.anyMessage.connect(handler);
  }

  private readonly onKernelMessage = (
    _: unknown,
    args: {
      direction: 'send' | 'recv';
      msg: {
        header: { msg_type: string };
        metadata?: Record<string, unknown>;
      };
    }
  ): void => {
    const { msg, direction } = args;
    if (direction === 'send' && msg.header.msg_type === 'execute_request') {
      // the panel's own check first (a replaced model is a resync), as before the lift
      this.process();
      this.session.beforeRun(msg.metadata?.cellId);
    }
  };

  private ensureIds(): void {
    const missing = this.shared.cells.filter(c => !c.getId() && c.setId);
    if (missing.length > 0) {
      this.shared.transact(() =>
        missing.forEach(c =>
          (c.setId as (id: string) => void)(this.deps.ids.uuid())
        )
      );
    }
  }

  private process(): void {
    if (!this.session.active) {
      return;
    }
    if (this.panel.context.model.sharedModel !== this.shared) {
      this.modelReplaced();
      return;
    }
    this.session.process();
  }

  private computeFingerprint(): Promise<NotebookRegistration['fingerprint']> {
    return notebookFingerprint(
      this.shared.cells.map(c => c.getSource()),
      `${this.shared.nbformat}.${this.shared.nbformat_minor}`,
      this.session.cellIdsStable
        ? this.shared.cells.map(c => c.getId())
        : undefined,
      this.deps.sha256Hex
    );
  }
}
