import type {
  Editor,
  NotebookRegistration,
  NotebookStatus,
  RecorderEvent
} from 'blaym-notebook-types';
import { alignCells } from './align';
import type { TipCell } from './align';
import { AuthorMarker } from './author';
import { EventBuilder } from './builder';
import { GapKeeper } from './gaps';
import { Heartbeat } from './heartbeat';
import { CellIdentity } from './ids';
import type { MetadataPolicy } from './metadata';
import { Outbox } from './outbox';
import type { OutboxReport } from './outbox';
import { Reconciler } from './reconcile';
import type { Transport } from './transport';
import { TypingGrouper } from './typing';
import type {
  AuthorMark,
  AuthorSignals,
  Clock,
  EventDraft,
  Ids,
  NotebookView,
  Scheduler
} from './types';
import { readView } from './view';

export type RecorderState =
  | { kind: 'starting' }
  | { kind: 'off' }
  | { kind: 'recording' }
  | {
      kind: 'inactive';
      reason: 'other_tab' | 'other_recorder' | 'collaboration';
    }
  | { kind: 'problem'; detail: string };

export interface RecorderSettings {
  heartbeatMs?: number;
  pauseMs?: number;
  maxMs?: number;
}

export interface RecorderSessionDeps {
  transport: Transport & { tip?(explorationId: string): Promise<TipCell[]> };
  clock: Clock;
  scheduler: Scheduler;
  ids: Ids;
  policy: MetadataPolicy;
  /** The editor named in every event and registration. */
  editor: Editor;
  /** The notebook's path now (it can change on a rename). */
  path: () => string;
  /** Called before the notebook is read, so every cell has an id. */
  ensureIds?: () => void;
  /** Used when the notebook metadata names no kernel. */
  kernelNameFallback?: () => string | undefined;
  /** Read at registration: the editor is in a collaboration mode. */
  collaboration?: () => boolean;
  settings?: RecorderSettings;
  /** The author signals (default: none). */
  signals?: AuthorSignals;
  /** The switching flag, read at event time. */
  switching?: () => boolean;
  onState?: (state: RecorderState) => void;
  onReport?: (report: OutboxReport) => void;
  onStatus?: (status: NotebookStatus) => void;
}

/** The editor-neutral part of recording one notebook: events, outbox, heartbeat, gaps. */
export class RecorderSession {
  readonly instanceId: string;
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
  private closed = false;
  private _state: RecorderState = { kind: 'starting' };
  private readonly disposers: Array<() => void> = [];

  constructor(
    private readonly view: NotebookView,
    private readonly deps: RecorderSessionDeps
  ) {
    this.instanceId = deps.ids.uuid();
    this.identity = new CellIdentity(view.cellIdsStable, deps.ids);
    this.reconciler = new Reconciler(deps.policy);
  }

  get state(): RecorderState {
    return this._state;
  }

  get cellIdsStable(): boolean {
    return this.identity.stable;
  }

  /** Recording is on and the session is built: editor changes are worth reading. */
  get active(): boolean {
    return !this.closed && !!this.grouper && this.recordingOn;
  }

  /** The editor's notebook was loaded (or reloaded): start the cell identity afresh. */
  resetIdentity(): void {
    this.identity = new CellIdentity(this.view.cellIdsStable, this.deps.ids);
  }

  setFingerprint(fingerprint: NotebookRegistration['fingerprint']): void {
    this.fingerprint = fingerprint;
  }

  /** Register with the service; a refusal is a `problem` state and no status. */
  async register(): Promise<NotebookStatus | undefined> {
    let status: NotebookStatus;
    try {
      status = await this.deps.transport.register(this.registration());
    } catch (e) {
      this.setState({ kind: 'problem', detail: String(e) });
      return undefined;
    }
    this.lastStatus = status;
    this.deps.onStatus?.(status);
    return status;
  }

  /** This session is the recorder: build the parts and start the heartbeat. */
  async start(notebookId: string): Promise<void> {
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

  /** Send what is queued; a `pagehide` also closes the heartbeat cleanly. */
  flushKeepalive(close: boolean): void {
    if (!this.grouper) {
      return;
    }
    this.grouper.flushAll();
    const outbox = this.outbox;
    void Promise.all(this.pending).then(() => outbox?.flushKeepalive());
    if (close) {
      this.heartbeat?.close();
    }
  }

  /** The editor's model object was replaced: a resync. */
  modelReplaced(): void {
    if (this.recordingOn) {
      this.outbox?.requestResync('model_replaced');
    }
  }

  /** The user cleared the outputs of a cell (the editor's id). */
  outputsCleared(modelCellId: string): void {
    const cellId = this.identity.blaymId(modelCellId);
    this.queue(
      { type: 'cell_outputs_cleared', payload: { cellId }, cellId },
      { hint: 'unknown' }
    );
  }

  /** A cell's Blaym id and its revision once the typing queued so far is built. */
  async cellRevision(
    modelCellId: string
  ): Promise<{ cellId: string; revision: number } | undefined> {
    await Promise.all(this.pending);
    const cellId = this.identity.blaymId(modelCellId);
    const revision = this.builder?.revisionOf(cellId);
    return revision === undefined ? undefined : { cellId, revision };
  }

  close(): void {
    this.closed = true;
    this.disposers.splice(0).forEach(d => d());
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
      editor: d.editor,
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
      editor: d.editor,
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

  /* --------------------------- the recorder -------------------------- */

  /** An outgoing run request: the code about to run is flushed first. */
  beforeRun(modelCellId: unknown): void {
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

  process(): void {
    const grouper = this.grouper;
    if (this.closed || !grouper || !this.recordingOn) {
      return;
    }
    if (this.deps.switching?.() || this.outbox?.needsResync) {
      return;
    }
    this.deps.ensureIds?.();
    const r = this.reconciler.reconcile(this.view, this.identity);
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

  /** Flush the typing in progress (a save started). */
  flushTyping(): void {
    this.grouper?.flushAll();
  }

  queue(draft: EventDraft, author?: AuthorMark): void {
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
  snapshotNow(): Promise<RecorderEvent> {
    const builder = this.builder as EventBuilder;
    this.deps.ensureIds?.();
    if (this.lastStatus) {
      builder.noteStatus(this.lastStatus.cellRevisions);
    }
    const view = this.view;
    const v = readView(view, this.identity, this.deps.policy);
    const meta = view.notebookMetadata() as {
      kernelspec?: { name?: string; language?: string };
      language_info?: { name?: string };
    };
    const kernelName =
      meta.kernelspec?.name ?? this.deps.kernelNameFallback?.() ?? 'unknown';
    const language =
      meta.language_info?.name ?? meta.kernelspec?.language ?? 'unknown';
    const p = builder.snapshot({
      path: this.deps.path(),
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
      path: this.deps.path(),
      editor: this.deps.editor,
      instanceId: this.instanceId,
      cellIdsStable: this.identity.stable,
      fingerprint: this.fingerprint as NotebookRegistration['fingerprint'],
      cellAliases: aliases,
      ...(this.deps.collaboration?.() ? { collaboration: true } : {})
    };
  }

  private reregisterIfAliasesChanged(): void {
    if (JSON.stringify(this.identity.aliases()) !== this.sentAliases) {
      this.deps.transport.register(this.registration()).catch(() => undefined);
    }
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

  /** Page-open alignment with the recorded tip, then the aliases go to the server. */
  private async align(): Promise<void> {
    const status = this.lastStatus as NotebookStatus;
    const tip = (await this.deps.transport.tip?.(status.notebookId)) ?? [];
    const view = this.view.cells();
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
    if (r.kind === 'dropped') {
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
    if (this.recordingOn && this._state.kind === 'problem') {
      this.setState({ kind: 'recording' });
    }
  }

  setState(state: RecorderState): void {
    this._state = state;
    this.deps.onState?.(state);
  }
}

/** The registration fingerprint: the same rule for every editor. */
export async function notebookFingerprint(
  sources: string[],
  nbformat: string,
  cellIds: string[] | undefined,
  sha256Hex: (text: string) => Promise<string>
): Promise<NotebookRegistration['fingerprint']> {
  const fp: NotebookRegistration['fingerprint'] = {
    nbformat,
    sourceHash: await sha256Hex(JSON.stringify(sources))
  };
  if (cellIds) {
    fp.cellIds = cellIds;
  }
  return fp;
}
