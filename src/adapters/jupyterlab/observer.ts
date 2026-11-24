import type {
  CellType,
  NotebookRegistration,
  NotebookStatus,
  RecorderEvent
} from 'blaym-jupyter-types';
import { alignCells } from '../../recording/align';
import type { TipCell } from '../../recording/align';
import { EventBuilder } from '../../recording/builder';
import { CellIdentity } from '../../recording/ids';
import { Heartbeat } from '../../recording/heartbeat';
import type { MetadataPolicy } from '../../recording/metadata';
import { Outbox } from '../../recording/outbox';
import type { OutboxReport } from '../../recording/outbox';
import { Reconciler } from '../../recording/reconcile';
import type { Transport } from '../../recording/transport';
import { TypingGrouper } from '../../recording/typing';
import { AuthorMarker } from '../../recording/author';
import type {
  AuthorMark,
  AuthorSignals,
  CellView,
  Clock,
  EventDraft,
  Ids,
  NotebookView,
  Scheduler,
  TabElection
} from '../../recording/types';
import { readView } from '../../recording/view';

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

/** A cell's output-area model (its `changed` fires for appended and cleared outputs). */
export interface OutputsLike {
  length: number;
  changed: SignalLike;
}

export interface CellModelLike {
  sharedModel: SharedCellLike;
  outputs?: OutputsLike;
}

export interface ContextLike {
  path: string;
  ready: Promise<void>;
  model: {
    sharedModel: SharedNotebookLike;
    collaborative?: boolean;
    cells?: Iterable<CellModelLike>;
  };
  saveState: SignalLike<string>;
  disposed: SignalLike;
  sessionContext: SessionContextLike;
}

export interface PanelLike {
  context: ContextLike;
}

/* ------------------------------------------------------------------ */

export type ObserverState =
  | { kind: 'starting' }
  | { kind: 'off' }
  | { kind: 'recording' }
  | {
      kind: 'inactive';
      reason: 'other_tab' | 'other_recorder' | 'collaboration';
    }
  | { kind: 'problem'; detail: string };

export interface ObserverSettings {
  heartbeatMs?: number;
  pauseMs?: number;
  maxMs?: number;
}

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
  /** Task 19: the author signals (default: none). */
  signals?: AuthorSignals;
  /** The switching flag (S5-P): never set in S5; read at event time. */
  switching?: () => boolean;
  /** Task 16 seams. */
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
  private shared: SharedNotebookLike;
  private identity: CellIdentity;
  private readonly reconciler: Reconciler;
  private builder?: EventBuilder;
  private grouper?: TypingGrouper;
  private outbox?: Outbox;
  private heartbeat?: Heartbeat;
  private lastStatus?: NotebookStatus;
  private fingerprint?: NotebookRegistration['fingerprint'];
  private sentAliases = '{}';
  private recordingOn = false;
  private starting = false;
  private pending: Array<Promise<unknown>> = [];
  private running = new Set<string>();
  private runByMsg = new Map<string, string>();
  private cellHandlers = new Map<SharedCellLike, () => void>();
  private outputHandlers = new Map<OutputsLike, () => void>();
  private kernel?: KernelLike | null;
  private disposed = false;
  private state: ObserverState = { kind: 'starting' };
  private readonly disposers: Array<() => void> = [];

  constructor(
    private readonly panel: PanelLike,
    private readonly deps: ObserverDeps
  ) {
    this.instanceId = deps.ids.uuid();
    this.shared = panel.context.model.sharedModel;
    this.identity = new CellIdentity(this.stable(), deps.ids);
    this.reconciler = new Reconciler(deps.policy);
  }

  get currentState(): ObserverState {
    return this.state;
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
    this.identity = new CellIdentity(this.stable(), this.deps.ids);
    this.ensureIds();
    this.attach();
    this.fingerprint = await this.computeFingerprint();
    let status: NotebookStatus;
    try {
      status = await this.deps.transport.register(this.registration());
    } catch (e) {
      this.setState({ kind: 'problem', detail: String(e) });
      return;
    }
    this.lastStatus = status;
    const notebookId = status.notebookId;
    this.deps.onStatus?.(status);
    if (status.reason === 'collaboration_not_supported') {
      this.setState({ kind: 'inactive', reason: 'collaboration' });
      return;
    }
    if ((await this.deps.election.acquire(notebookId)) === 'inactive') {
      this.setState({ kind: 'inactive', reason: 'other_tab' });
      void this.waitForTakeover(notebookId);
      return;
    }
    if (this.disposed) {
      this.deps.election.releaseLock?.();
      return;
    }
    this.build(notebookId);
    await this.heartbeat?.start();
  }

  /** A tab that lost the election takes over when the holder goes away (S5-J). */
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
    this.build(notebookId);
    await this.heartbeat?.start();
  }

  /** Switch recording on or off for this notebook (the registry holds the flag). */
  async setRecording(on: boolean): Promise<void> {
    try {
      const status = await this.deps.transport.register({
        ...this.registration(),
        setRecording: on
      });
      this.onStatus(status);
    } catch (e) {
      this.setState({ kind: 'problem', detail: String(e) });
    }
  }

  /** `visibilitychange` to hidden: send what is queued. `pagehide`: also a clean close. */
  hide(kind: 'hidden' | 'pagehide'): void {
    if (!this.grouper) {
      return;
    }
    this.grouper.flushAll();
    const outbox = this.outbox;
    void Promise.all(this.pending).then(() => outbox?.flushKeepalive());
    if (kind === 'pagehide') {
      this.heartbeat?.close();
    }
  }

  /** The model object behind the panel was replaced (revert, reload): a resync. */
  modelReplaced(): void {
    this.shared = this.panel.context.model.sharedModel;
    this.attach();
    if (this.recordingOn) {
      this.outbox?.requestResync('model_replaced');
    }
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
    this.outputHandlers.forEach((h, o) => o.changed.disconnect(h));
    this.outputHandlers.clear();
    this.heartbeat?.stop();
    this.outbox?.close();
  }

  /* ----------------------------- wiring ----------------------------- */

  private build(notebookId: string): void {
    const d = this.deps;
    const builder = new EventBuilder({
      clock: d.clock,
      ids: d.ids,
      transport: d.transport,
      explorationId: notebookId,
      editor: 'jupyterlab',
      originId: this.instanceId,
      policy: d.policy
    });
    this.builder = builder;
    const marker = d.signals
      ? new AuthorMarker({
          signals: d.signals,
          clock: d.clock,
          scheduler: d.scheduler,
          boundary: id =>
            id ? this.grouper?.boundary(id) : this.grouper?.flushAll()
        })
      : undefined;
    if (marker) {
      this.disposers.push(() => marker.close());
    }
    this.grouper = new TypingGrouper({
      scheduler: d.scheduler,
      clock: d.clock,
      pauseMs: d.settings?.pauseMs,
      maxMs: d.settings?.maxMs,
      emit: (draft, author) => this.queue(draft, author),
      mark: (id, from, to) =>
        marker ? marker.mark(id, from, to) : { hint: 'unknown' }
    });
    this.outbox = new Outbox({
      transport: d.transport,
      scheduler: d.scheduler,
      clock: d.clock,
      explorationId: notebookId,
      editor: 'jupyterlab',
      instanceId: this.instanceId,
      snapshotter: () => this.snapshotNow(),
      onReport: r => this.onReport(r)
    });
    this.heartbeat = new Heartbeat({
      transport: d.transport,
      scheduler: d.scheduler,
      notebookId,
      instanceId: this.instanceId,
      heartbeatMs: d.settings?.heartbeatMs ?? 15000,
      registration: () => this.registration(),
      onStatus: s => this.onStatus(s),
      onActive: active => {
        if (active) {
          this.outbox?.resume();
        } else {
          this.recordingOn = false;
          this.setState({ kind: 'inactive', reason: 'other_recorder' });
        }
      },
      onResync: reason => {
        if (this.recordingOn) {
          this.outbox?.requestResync(reason);
        }
      },
      snapshotPending: () => this.outbox?.needsResync ?? false,
      closeGap: gap =>
        this.queue({
          type: 'recording_gap_ended',
          payload: { gapId: gap.gapId }
        }),
      onError: (kind, e) =>
        this.setState({ kind: 'problem', detail: `${kind}: ${String(e)}` })
    });
  }

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
          this.grouper?.flushAll();
        }
      };
      ctx.saveState.connect(onSave);
      this.disposers.push(() => ctx.saveState.disconnect(onSave));
      const onKernel = (): void => this.subscribeKernel();
      ctx.sessionContext.kernelChanged.connect(onKernel);
      const onConnection = (_: unknown, status?: unknown): void => {
        if (status !== undefined && status !== 'connected') {
          this.clearRuns();
        }
        this.subscribeKernel();
      };
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

  /**
   * The shared cell's `changed` does not fire for outputs (seen in a real page),
   * so a user clearing outputs is found through the output-area models. Only an
   * EMPTY list outside a run is looked at: appended output of a run would flood
   * the reconciler.
   */
  private syncOutputHandlers(): void {
    const cells = this.panel.context.model.cells;
    if (!cells) {
      return;
    }
    const live = new Map<OutputsLike, SharedCellLike>();
    for (const c of cells) {
      if (c.outputs) {
        live.set(c.outputs, c.sharedModel);
      }
    }
    this.outputHandlers.forEach((h, o) => {
      if (!live.has(o)) {
        o.changed.disconnect(h);
        this.outputHandlers.delete(o);
      }
    });
    live.forEach((cell, outputs) => {
      if (this.outputHandlers.has(outputs)) {
        return;
      }
      const h = (): void => {
        if (
          outputs.length === 0 &&
          !this.running.has(this.identity.blaymId(cell.getId()))
        ) {
          this.process();
        }
      };
      outputs.changed.connect(h);
      this.outputHandlers.set(outputs, h);
    });
  }

  private syncCellHandlers(): void {
    this.syncOutputHandlers();
    const live = new Set(this.shared.cells);
    this.cellHandlers.forEach((h, c) => {
      if (!live.has(c)) {
        c.changed.disconnect(h);
        this.cellHandlers.delete(c);
      }
    });
    live.forEach(c => {
      if (!this.cellHandlers.has(c)) {
        const h = (): void => this.process();
        c.changed.connect(h);
        this.cellHandlers.set(c, h);
      }
    });
  }

  private subscribeKernel(): void {
    const kernel = this.panel.context.sessionContext.session?.kernel ?? null;
    if (kernel === this.kernel) {
      return;
    }
    const handler = this.onKernelMessage;
    this.kernel?.anyMessage.disconnect(handler);
    this.kernel = kernel;
    this.clearRuns(); // the old kernel's replies will never arrive
    kernel?.anyMessage.connect(handler);
  }

  /** Forget the runs in flight (kernel replaced, restarted or disconnected). */
  private clearRuns(): void {
    this.running.clear();
    this.runByMsg.clear();
  }

  private readonly onKernelMessage = (
    _: unknown,
    args: {
      direction: 'send' | 'recv';
      msg: {
        header: { msg_type: string; msg_id?: string };
        parent_header?: { msg_id?: string };
        metadata?: Record<string, unknown>;
      };
    }
  ): void => {
    const { msg, direction } = args;
    if (direction === 'send' && msg.header.msg_type === 'execute_request') {
      this.beforeRun(msg.header.msg_id, msg.metadata?.cellId);
    } else if (
      direction === 'recv' &&
      msg.header.msg_type === 'execute_reply'
    ) {
      const parent = msg.parent_header?.msg_id;
      const cell = parent ? this.runByMsg.get(parent) : undefined;
      if (parent && cell !== undefined) {
        this.runByMsg.delete(parent);
        this.running.delete(cell);
      }
    }
  };

  /** An outgoing `execute_request` (S5-K): the code about to run is flushed first. */
  private beforeRun(msgId: string | undefined, modelCellId: unknown): void {
    if (!this.recordingOn || !this.grouper) {
      return;
    }
    this.process();
    if (typeof modelCellId !== 'string') {
      this.grouper.flushAll();
      return;
    }
    const id = this.identity.blaymId(modelCellId);
    this.running.add(id);
    if (msgId) {
      this.runByMsg.set(msgId, id);
    }
    this.grouper.beforeRun(id);
  }

  /* --------------------------- the recorder -------------------------- */

  private view(): NotebookView {
    const shared = this.shared;
    return {
      cellIdsStable: this.identity.stable,
      notebookMetadata: () => shared.getMetadata(),
      cells: (): CellView[] =>
        shared.cells.map(c => ({
          id: c.getId(),
          type: c.cell_type as CellType,
          source: c.getSource(),
          metadata: c.getMetadata(),
          outputs: c.cell_type === 'code' ? (c.getOutputs?.() ?? []) : []
        }))
    };
  }

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
    const grouper = this.grouper;
    if (this.disposed || !grouper || !this.recordingOn) {
      return;
    }
    if (this.panel.context.model.sharedModel !== this.shared) {
      this.modelReplaced();
      return;
    }
    if (this.deps.switching?.() || this.outbox?.needsResync) {
      return;
    }
    this.ensureIds();
    const r = this.reconciler.reconcile(this.view(), this.identity, {
      isRunning: id => this.running.has(id)
    });
    if (r.kind === 'resync') {
      this.outbox?.requestResync(`reconcile:${r.reason}`);
      return;
    }
    r.edits.forEach(e => grouper.edit(e.cellId, e.code));
    r.flushBefore.forEach(id => grouper.flushCell(id));
    for (const d of r.drafts) {
      this.queue(d, { hint: 'unknown' });
      if (d.type === 'cell_inserted') {
        grouper.baseline(d.cellId as string, String(d.payload.code));
      }
      if (d.type === 'cell_deleted') {
        grouper.forget(d.cellId as string);
      }
    }
    this.reregisterIfAliasesChanged();
  }

  private queue(draft: EventDraft, author?: AuthorMark): void {
    const builder = this.builder;
    if (!builder || !this.recordingOn) {
      return;
    }
    const p = builder.build(draft, author).then(
      (ev: RecorderEvent) => this.outbox?.enqueue(ev),
      e => this.setState({ kind: 'problem', detail: String(e) })
    );
    this.pending.push(p);
    void p.then(() => {
      this.pending = this.pending.filter(x => x !== p);
    });
  }

  /** The snapshotter of the outbox: the notebook as it is now, and the reconciler reset to it. */
  private snapshotNow(): Promise<RecorderEvent> {
    const builder = this.builder as EventBuilder;
    this.ensureIds();
    if (this.lastStatus) {
      builder.noteStatus(this.lastStatus.cellRevisions);
    }
    const view = this.view();
    const v = readView(view, this.identity, this.deps.policy);
    const meta = view.notebookMetadata() as {
      kernelspec?: { name?: string; language?: string };
      language_info?: { name?: string };
    };
    const kernelName =
      meta.kernelspec?.name ??
      this.panel.context.sessionContext.session?.kernel?.name ??
      'unknown';
    const language =
      meta.language_info?.name ?? meta.kernelspec?.language ?? 'unknown';
    const p = builder.snapshot({
      path: this.panel.context.path,
      notebookMetadata: view.notebookMetadata(),
      kernelName,
      language,
      cells: v.cells
    });
    this.reconciler.reset(view, this.identity);
    v.cells.forEach(c => this.grouper?.baseline(c.id, c.code));
    return p;
  }

  /* ------------------------ status and registration ------------------ */

  private registration(): NotebookRegistration {
    const aliases = this.identity.aliases();
    this.sentAliases = JSON.stringify(aliases);
    return {
      path: this.panel.context.path,
      editor: 'jupyterlab',
      instanceId: this.instanceId,
      cellIdsStable: this.identity.stable,
      fingerprint: this.fingerprint as NotebookRegistration['fingerprint'],
      cellAliases: aliases,
      ...(this.panel.context.model.collaborative ? { collaboration: true } : {})
    };
  }

  private reregisterIfAliasesChanged(): void {
    if (JSON.stringify(this.identity.aliases()) !== this.sentAliases) {
      this.deps.transport.register(this.registration()).catch(() => undefined);
    }
  }

  private async computeFingerprint(): Promise<
    NotebookRegistration['fingerprint']
  > {
    const sources = this.shared.cells.map(c => c.getSource());
    const fp: NotebookRegistration['fingerprint'] = {
      nbformat: `${this.shared.nbformat}.${this.shared.nbformat_minor}`,
      sourceHash: await this.deps.sha256Hex(JSON.stringify(sources))
    };
    if (this.identity.stable) {
      fp.cellIds = this.shared.cells.map(c => c.getId());
    }
    return fp;
  }

  private onStatus(status: NotebookStatus): void {
    this.lastStatus = status;
    this.deps.onStatus?.(status);
    if (!status.recording) {
      this.recordingOn = false;
      this.setState({ kind: 'off' });
    } else if (status.active) {
      void this.startRecording();
    }
  }

  private async startRecording(): Promise<void> {
    if (this.recordingOn || this.starting || !this.builder || !this.outbox) {
      return;
    }
    this.starting = true;
    try {
      if (!this.identity.stable) {
        await this.align();
      }
      this.recordingOn = true;
      this.setState({ kind: 'recording' });
      this.outbox.requestResync('start');
    } catch (e) {
      this.setState({ kind: 'problem', detail: String(e) });
    } finally {
      this.starting = false;
    }
  }

  /** Page-open alignment with the recorded tip (S5-U), then the aliases go to the server. */
  private async align(): Promise<void> {
    const status = this.lastStatus as NotebookStatus;
    const tip = (await this.deps.transport.tip?.(status.notebookId)) ?? [];
    const view = this.view().cells();
    const result = alignCells(
      view.map(c => ({ id: c.id, type: c.type, source: c.source })),
      tip
    );
    Object.entries(result.matches).forEach(([modelId, blaymId]) =>
      this.identity.setAlias(modelId, blaymId)
    );
    this.builder?.noteStatus(status.cellRevisions);
    this.builder?.noteTip(tip);
    await this.deps.transport.register(this.registration());
  }

  private onReport(r: OutboxReport): void {
    if (r.kind === 'paused' && r.reason === 'not_active_recorder') {
      this.recordingOn = false;
      this.setState({ kind: 'inactive', reason: 'other_recorder' });
    } else if (r.kind === 'paused') {
      this.setState({ kind: 'problem', detail: r.reason });
    } else if (r.kind === 'refused') {
      this.setState({ kind: 'problem', detail: r.reason });
    } else if (r.kind === 'rejected') {
      this.setState({ kind: 'problem', detail: `http ${r.status}` });
    }
    this.deps.onReport?.(r);
  }

  private setState(state: ObserverState): void {
    this.state = state;
    this.deps.onState?.(state);
  }
}
