import type {
  Editor,
  IngestOutcome,
  IngestRefusalReason,
  RecorderEvent
} from 'blaym-jupyter-types';
import { TransportError } from './transport';
import type { Transport } from './transport';
import type { Cancel, Clock, Scheduler } from './types';

export type OutboxReport =
  | {
      kind: 'paused';
      reason: 'not_active_recorder' | 'unauthorized' | 'snapshot_refused';
      detail?: string;
    }
  | {
      kind: 'refused';
      reason: IngestRefusalReason;
      detail: string;
      eventId: string;
    }
  | { kind: 'retrying'; delayMs: number; error: string }
  | { kind: 'dropped'; count: number }
  | { kind: 'resync'; reason: string }
  | { kind: 'rejected'; status?: number };

export interface OutboxOptions {
  transport: Transport;
  scheduler: Scheduler;
  clock: Clock;
  explorationId: string;
  editor: Editor;
  instanceId: string;
  /** Builds a fresh snapshot of the notebook as it is now (and resets the reconciler to it). */
  snapshotter: () => Promise<RecorderEvent>;
  onReport?: (report: OutboxReport) => void;
  /** The branch of the last accepted outcome (S5-P). */
  onBranch?: (branchId: string) => void;
  maxBatchEvents?: number;
  maxBatchBytes?: number;
  maxQueued?: number;
  resyncMinIntervalMs?: number;
  snapshotRetryMs?: number;
}

/** Events a snapshot supersedes. Everything else (gaps, kernel and rename events) is kept. */
const CONTENT = new Set<string>([
  'cell_inserted',
  'cell_deleted',
  'cell_moved',
  'cell_type_changed',
  'cell_source_changed',
  'cell_metadata_changed',
  'notebook_metadata_changed'
]);

const SNAPSHOT_REFUSAL_LIMIT = 5;
const BACKOFF_FIRST = 500;
const BACKOFF_MAX = 8000;

function size(e: RecorderEvent): number {
  return JSON.stringify(e).length;
}

/**
 * The ordered queue to the server (memory only, never persistent storage).
 * Batches of at most 50 events or 200 KB; network errors retry at 0.5 s doubling
 * to 8 s; refusals are never retried except `run_in_progress` (the same
 * snapshot every 2 s, with everything behind it held back, because the queue
 * is sent strictly in order). A resync builds one snapshot, never more than one
 * every 5 s, and the snapshot replaces the queued content events ahead of it.
 */
export class Outbox {
  private queue: RecorderEvent[] = [];
  private inFlight: Set<string> | null = null;
  private paused = false;
  private retryTimer?: Cancel;
  private backoff = BACKOFF_FIRST;
  private targetBranchId?: string;
  private resyncPending = false;
  private resyncTimer?: Cancel;
  private snapshotId?: string;
  private lastResyncAt = Number.NEGATIVE_INFINITY;
  private closed = false;
  private refusedSnapshots = 0;

  constructor(private readonly o: OutboxOptions) {}

  get length(): number {
    return this.queue.length;
  }

  get isPaused(): boolean {
    return this.paused;
  }

  /** A resync is wanted or its snapshot is not yet accepted. */
  get needsResync(): boolean {
    return this.resyncPending || this.snapshotId !== undefined;
  }

  enqueue(event: RecorderEvent): void {
    this.queue.push(event);
    this.enforceBound();
    this.pump();
  }

  /** The tab became active again (or the cause of a pause is gone). */
  resume(): void {
    const gaveUp = this.refusedSnapshots >= SNAPSHOT_REFUSAL_LIMIT;
    this.paused = false;
    if (gaveUp) {
      this.refusedSnapshots = 0;
      this.startResync('resumed');
    }
    this.pump();
  }

  retarget(branchId: string | undefined): void {
    this.targetBranchId = branchId;
  }

  /** Ask for a fresh snapshot. At most one is ever wanted or waiting; at most one per 5 s is built. */
  requestResync(reason: string): void {
    this.refusedSnapshots = 0;
    this.startResync(reason);
  }

  private startResync(reason: string): void {
    if (this.closed || this.resyncPending || this.snapshotId !== undefined) {
      return;
    }
    this.resyncPending = true;
    this.o.onReport?.({ kind: 'resync', reason });
    const wait = Math.max(
      0,
      this.lastResyncAt +
        (this.o.resyncMinIntervalMs ?? 5000) -
        this.o.clock.nowMs()
    );
    this.resyncTimer = this.o.scheduler.setTimeout(() => {
      this.resyncTimer = undefined;
      void this.sendSnapshot();
    }, wait);
  }

  /** The page hides: hand what is queued to a keepalive-capable call (duplicates are ignored by the server). */
  flushKeepalive(): void {
    const send = this.o.transport.ingestKeepalive;
    if (!send || this.paused || this.queue.length === 0) {
      return;
    }
    const batch = this.takeBatch();
    send.call(this.o.transport, this.request(batch));
  }

  close(): void {
    this.closed = true;
    this.retryTimer?.();
    this.resyncTimer?.();
  }

  private request(events: RecorderEvent[]) {
    return {
      explorationId: this.o.explorationId,
      ...(this.targetBranchId ? { targetBranchId: this.targetBranchId } : {}),
      editor: this.o.editor,
      instanceId: this.o.instanceId,
      events
    };
  }

  private async sendSnapshot(): Promise<void> {
    if (this.closed) {
      return;
    }
    let snapshot: RecorderEvent;
    // Only events queued now are superseded; later ones may not be in the snapshot.
    const before = new Set(this.queue.map(e => e.eventId));
    try {
      snapshot = await this.o.snapshotter();
    } catch (e) {
      // Building failed (for example prepareOutputs was unreachable): try again later.
      this.resyncPending = false;
      this.lastResyncAt = this.o.clock.nowMs();
      this.o.onReport?.({ kind: 'retrying', delayMs: 5000, error: String(e) });
      this.startResync('snapshot_failed');
      return;
    }
    this.resyncPending = false;
    this.lastResyncAt = this.o.clock.nowMs();
    const inFlight = this.inFlight;
    this.queue = this.queue.filter(
      e =>
        !CONTENT.has(e.type) ||
        !before.has(e.eventId) ||
        (inFlight !== null && inFlight.has(e.eventId))
    );
    this.snapshotId = snapshot.eventId;
    const firstLate = this.queue.findIndex(e => !before.has(e.eventId));
    if (firstLate < 0) {
      this.queue.push(snapshot);
    } else {
      this.queue.splice(firstLate, 0, snapshot);
    }
    this.pump();
  }

  private enforceBound(): void {
    const max = this.o.maxQueued ?? 5000;
    if (this.queue.length <= max) {
      return;
    }
    let dropped = 0;
    const inFlight = this.inFlight;
    for (let i = 0; i < this.queue.length && this.queue.length > max;) {
      const e = this.queue[i];
      if (CONTENT.has(e.type) && !(inFlight && inFlight.has(e.eventId))) {
        this.queue.splice(i, 1);
        dropped++;
      } else {
        i++;
      }
    }
    if (dropped > 0) {
      this.o.onReport?.({ kind: 'dropped', count: dropped });
      this.startResync('queue_overflow');
    }
  }

  private takeBatch(): RecorderEvent[] {
    const maxEvents = this.o.maxBatchEvents ?? 50;
    const maxBytes = this.o.maxBatchBytes ?? 200 * 1024;
    const batch: RecorderEvent[] = [];
    let bytes = 0;
    for (const e of this.queue) {
      const isSnapshot = e.type === 'notebook_snapshot';
      if (batch.length > 0 && isSnapshot) {
        break;
      }
      const n = size(e);
      if (
        batch.length > 0 &&
        (batch.length >= maxEvents || bytes + n > maxBytes)
      ) {
        break;
      }
      batch.push(e);
      bytes += n;
      if (isSnapshot) {
        break;
      }
    }
    return batch;
  }

  private pump(): void {
    if (
      this.closed ||
      this.paused ||
      this.inFlight !== null ||
      this.retryTimer !== undefined ||
      this.queue.length === 0
    ) {
      return;
    }
    const batch = this.takeBatch();
    this.inFlight = new Set(batch.map(e => e.eventId));
    this.o.transport.ingest(this.request(batch)).then(
      res => this.onResponse(batch, res.outcomes),
      err => this.onFailure(err)
    );
  }

  private afterAttempt(): void {
    this.inFlight = null;
  }

  private onFailure(err: unknown): void {
    this.afterAttempt();
    if (err instanceof TransportError && err.kind === 'http') {
      const status = err.status;
      if (status === 401 || status === 403) {
        this.paused = true;
        this.o.onReport?.({
          kind: 'paused',
          reason: 'unauthorized',
          detail: err.message
        });
        return;
      }
      if (status === undefined || status < 500) {
        // The server will not take this batch as it is: drop it and send a snapshot instead.
        const first = this.takeBatch();
        const ids = new Set(first.map(e => e.eventId));
        this.removeEvents(ids);
        this.o.onReport?.({ kind: 'rejected', status });
        this.startResync('rejected');
        this.pump();
        return;
      }
    }
    this.scheduleRetry(String(err), this.backoff);
    this.backoff = Math.min(this.backoff * 2, BACKOFF_MAX);
  }

  private scheduleRetry(error: string, delayMs: number): void {
    this.o.onReport?.({ kind: 'retrying', delayMs, error });
    this.retryTimer = this.o.scheduler.setTimeout(() => {
      this.retryTimer = undefined;
      this.pump();
    }, delayMs);
  }

  private removeEvents(ids: Set<string>): void {
    this.queue = this.queue.filter(e => {
      if (!ids.has(e.eventId)) {
        return true;
      }
      if (e.eventId === this.snapshotId) {
        this.snapshotId = undefined;
      }
      return false;
    });
  }

  private onResponse(batch: RecorderEvent[], outcomes: IngestOutcome[]): void {
    this.afterAttempt();
    if (outcomes.length !== batch.length) {
      this.scheduleRetry(
        'outcome count differs from the request',
        this.backoff
      );
      this.backoff = Math.min(this.backoff * 2, BACKOFF_MAX);
      return;
    }
    this.backoff = BACKOFF_FIRST;
    const remove = new Set<string>();
    let pausedFor: 'not_active_recorder' | 'unauthorized' | undefined;
    let holdSnapshot = false;
    let resync: string | undefined;
    outcomes.forEach((out, i) => {
      const event = batch[i];
      if (out.status === 'accepted') {
        if (event.type === 'notebook_snapshot') {
          this.refusedSnapshots = 0;
        }
        remove.add(event.eventId);
        this.targetBranchId = out.branchId;
        this.o.onBranch?.(out.branchId);
        return;
      }
      this.o.onReport?.({
        kind: 'refused',
        reason: out.reason,
        detail: out.detail,
        eventId: out.eventId
      });
      switch (out.reason) {
        case 'not_active_recorder':
        case 'unauthorized':
          pausedFor = out.reason;
          break;
        case 'run_in_progress':
          holdSnapshot = true;
          break;
        case 'unknown_branch':
          this.targetBranchId = undefined;
          remove.add(event.eventId);
          resync = 'unknown_branch';
          break;
        case 'bad_first_event':
          remove.add(event.eventId);
          resync = 'bad_first_event';
          break;
        default:
          if (event.type === 'notebook_snapshot') {
            this.refusedSnapshots++;
          }
          // invalid_event, conflict, unsupported_schema_version, missing_blob
          remove.add(event.eventId);
          resync = out.reason;
      }
    });
    this.removeEvents(remove);
    if (pausedFor !== undefined) {
      this.paused = true;
      this.o.onReport?.({ kind: 'paused', reason: pausedFor });
      return;
    }
    if (this.refusedSnapshots >= SNAPSHOT_REFUSAL_LIMIT) {
      this.paused = true;
      this.o.onReport?.({ kind: 'paused', reason: 'snapshot_refused' });
      return;
    }
    if (resync !== undefined) {
      this.startResync(resync);
    }
    if (holdSnapshot) {
      this.scheduleRetry('run_in_progress', this.o.snapshotRetryMs ?? 2000);
      return;
    }
    this.pump();
  }
}
