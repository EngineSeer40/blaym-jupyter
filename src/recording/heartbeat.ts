import type {
  NotebookRegistration,
  NotebookStatus,
  OpenGap
} from 'blaym-jupyter-types';
import type { Transport } from './transport';
import type { Cancel, Scheduler } from './types';

export interface HeartbeatOptions {
  transport: Transport;
  scheduler: Scheduler;
  notebookId: string;
  instanceId: string;
  /** Builds the registration (with the current aliases); used at start and on `needsRegister`. */
  registration: () => NotebookRegistration;
  heartbeatMs?: number;
  /** Every answer (register or status), first. */
  onStatus?: (status: NotebookStatus) => void;
  /** Whether this instance is the active recorder; called when it changes. */
  onActive: (active: boolean, status: NotebookStatus) => void;
  /** The server asks for a fresh snapshot. */
  onResync: (reason: 'server' | 'became_active') => void;
  /** True while a snapshot is wanted or not yet accepted: gaps are closed only after it. */
  snapshotPending: () => boolean;
  /** Close a gap the browser opened (enqueue its `recording_gap_ended`). */
  closeGap: (gap: OpenGap) => void;
  onError?: (kind: 'register' | 'status', error: unknown) => void;
}

/**
 * The recorder's heartbeat (S5-S): `status` every `heartbeatMs` (15 s) and right
 * after register. It reacts to `needsRegister` (register again), `needsResync`,
 * `active` (a recorder that becomes active resyncs) and `openGaps` (the gaps
 * the browser opened are closed once its snapshot is out). `close()` ends it
 * cleanly and tells the server.
 */
export class Heartbeat {
  private timer?: Cancel;
  private ended = false;
  private busy = false;
  private active: boolean | undefined;
  private closedGaps = new Set<string>();
  private readonly ms: number;

  constructor(private readonly o: HeartbeatOptions) {
    this.ms = o.heartbeatMs ?? 15000;
  }

  async start(): Promise<void> {
    await this.register();
    await this.beat();
    this.schedule();
  }

  /** A clean end: stops, and tells the server so the next start can tell it from a lost session. */
  close(): void {
    this.stop();
    this.o.transport.close(this.o.notebookId, this.o.instanceId);
  }

  /** Stops without telling the server. */
  stop(): void {
    this.ended = true;
    if (this.timer) {
      this.timer();
      this.timer = undefined;
    }
  }

  private schedule(): void {
    if (this.ended) {
      return;
    }
    this.timer = this.o.scheduler.setTimeout(() => {
      void this.beat().then(() => this.schedule());
    }, this.ms);
  }

  private async register(): Promise<void> {
    try {
      this.handle(await this.o.transport.register(this.o.registration()));
    } catch (e) {
      this.o.onError?.('register', e);
    }
  }

  private async beat(): Promise<void> {
    if (this.busy || this.ended) {
      return;
    }
    this.busy = true;
    try {
      const status = await this.o.transport.status(
        this.o.notebookId,
        this.o.instanceId
      );
      if (status.needsRegister) {
        await this.register();
      } else {
        this.handle(status);
      }
    } catch (e) {
      this.o.onError?.('status', e);
    } finally {
      this.busy = false;
    }
  }

  private handle(status: NotebookStatus): void {
    if (this.ended) {
      return;
    }
    this.o.onStatus?.(status);
    const was = this.active;
    this.active = status.active;
    if (was !== status.active) {
      this.o.onActive(status.active, status);
    }
    if (!status.active) {
      return;
    }
    if (status.needsResync) {
      this.o.onResync('server');
    } else if (was === false) {
      this.o.onResync('became_active');
    }
    if (status.openGaps.length > 0 && !this.o.snapshotPending()) {
      status.openGaps
        .filter(g => g.source === 'browser' && !this.closedGaps.has(g.gapId))
        .forEach(g => {
          this.closedGaps.add(g.gapId);
          this.o.closeGap(g);
        });
    }
  }
}
