import type {
  CellType,
  NotebookRegistration,
  NotebookStatus,
  RecorderEvent
} from 'blaym-jupyter-types';
import { alignCells } from '../../recording/align';
import type { TipCell } from '../../recording/align';
import { EventBuilder } from '../../recording/builder';
import { GapKeeper } from '../../recording/gaps';
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
  private gaps?: GapKeeper;
  private lastStatus?: NotebookStatus;
  private fingerprint?: NotebookRegistration['fingerprint'];
  private sentAliases = '{}';
  private recordingOn = false;
  private starting = false;
  /** The `switched_off` gap is open: no snapshot closes it until recording is on again. */
  private goingOff = false;
  private pending: Array<Promise<unknown>> = [];
  private cellHandlers = new Map<SharedCellLike, () => void>();
  /**
   * Model ids of the cells that have shown output since the user last cleared
   * them. A clear command is reported after the output is already gone.
   */
  private hadOutput = new Set<string>();
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
      if (!on && this.recordingOn) {
        this.gaps?.open('switched_off');
        this.goingOff = true;
        await Promise.all(this.pending);
      }
      const status = await this.deps.transport.register({
        ...this.registration(),
        setRecording: on
      });
      this.onStatus(status);
    } catch (e) {
      // recording stays on: the next snapshot closes the gap
      this.goingOff = false;
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
        const cellId = this.identity.blaymId(id);
        this.queue(
          { type: 'cell_outputs_cleared', payload: { cellId }, cellId },
          { hint: 'unknown' }
        );
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
    this.gaps = new GapKeeper({
      ids: d.ids,
      emit: (draft, first) => this.emitGap(draft, first)
    });
    this.outbox = new Outbox({
      transport: d.transport,
      scheduler: d.scheduler,
      clock: d.clock,
      explorationId: notebookId,
      editor: 'jupyterlab',
      instanceId: this.instanceId,
      snapshotter: () => this.snapshotNow(),
      onBranch: () => this.onAccepted(),
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
      // while recording is off nothing closes a gap (its `switched_off` gap stays open)
      snapshotPending: () =>
        !this.recordingOn || (this.outbox?.needsResync ?? false),
      closeGap: gap => this.gaps?.close(gap),
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
      this.beforeRun(msg.metadata?.cellId);
    }
  };

  /** An outgoing `execute_request` (S5-K): the code about to run is flushed first. */
  private beforeRun(modelCellId: unknown): void {
    if (!this.recordingOn || !this.grouper) {
      return;
    }
    this.process();
    if (typeof modelCellId !== 'string') {
      this.grouper.flushAll();
      return;
    }
    const id = this.identity.blaymId(modelCellId);
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
    const r = this.reconciler.reconcile(this.view(), this.identity);
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

  /** A gap event: built and queued whether or not recording is on (the off gap is written as it goes off). */
  private emitGap(draft: EventDraft, first: boolean): void {
    const builder = this.builder;
    if (!builder) {
      return;
    }
    const p = builder.build(draft).then(
      (ev: RecorderEvent) => this.outbox?.enqueue(ev, first),
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
      this.goingOff = false;
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

  /**
   * Only a paused outbox is a problem the user sees. Other refusals and
   * rejections are handled by the outbox itself (retry or a fresh snapshot).
   */
  private onReport(r: OutboxReport): void {
    if (r.kind === 'held' || r.kind === 'dropped') {
      this.gaps?.open('unknown');
    } else if (r.kind === 'snapshot_accepted') {
      this.gaps?.closeAll(!this.goingOff);
    }
    if (r.kind === 'paused' && r.reason === 'not_active_recorder') {
      this.recordingOn = false;
      this.setState({ kind: 'inactive', reason: 'other_recorder' });
    } else if (r.kind === 'paused') {
      this.setState({ kind: 'problem', detail: r.reason });
    }
    this.deps.onReport?.(r);
  }

  /** The server took an event: whatever was wrong is over. */
  private onAccepted(): void {
    if (this.recordingOn && this.state.kind === 'problem') {
      this.setState({ kind: 'recording' });
    }
  }

  private setState(state: ObserverState): void {
    this.state = state;
    this.deps.onState?.(state);
  }
}
