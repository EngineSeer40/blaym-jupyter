import type { Transport } from './transport';
import type { RecorderSession } from './session';
import type { Cancel, Clock, Ids, Scheduler } from './types';

/** What the editor shows of one cell after an execution change. */
export interface CellObservation {
  /** The editor's cell id. */
  cellId: string;
  executionOrder?: number;
  /** Set once the run has finished. */
  success?: boolean;
  errorName?: string;
  /** The cell's whole output list now (raw nbformat). */
  outputs: Record<string, unknown>[];
}

export type KernelObservation =
  | { state: 'started'; kernelName: string; language: string }
  | { state: 'restarting' }
  | { state: 'dead' };

export interface RunTrackerOptions {
  session: Pick<
    RecorderSession,
    'queue' | 'beforeRun' | 'cellRevision' | 'outputsCleared'
  >;
  transport: Pick<Transport, 'prepareOutputs'>;
  explorationId: () => string;
  clock: Clock;
  scheduler: Scheduler;
  ids: Ids;
  /** Kernel name and language, for the `kernel_started` written before the first run. */
  kernelInfo?: () => { kernelName: string; language: string };
  /** Output changes of a cell are sent together after this long (default 250). */
  coalesceMs?: number;
}

interface Run {
  runId: string;
  /** Set when `run_started` is written; a run that could not start stays false. */
  cellId?: string;
  alive: boolean;
  latest: Record<string, unknown>[];
  sent: string[];
  timer?: Cancel;
}

const same = (a: string[], b: string[]): boolean =>
  a.length === b.length && a.every((x, i) => x === b[i]);

/**
 * Turns what an editor shows of a cell (its execution order, success and whole
 * output list) into `run_started`, `output_*`, `outputs_cleared`,
 * `cell_outputs_cleared`, `run_finished` and kernel events. Everything goes
 * through the session's queue in the order seen. A kernel restart or crash gives
 * its open runs no `run_finished`: the rebuilder marks them unfinished.
 */
export class RunTracker {
  private readonly open = new Map<string, Run>();
  private readonly lastOrder = new Map<string, number>();
  private readonly seenOutputs = new Map<string, number>();
  private chain: Promise<unknown> = Promise.resolve();
  private kernelId?: string;
  /** After `restarting` or `dead`: the next `started` is the new kernel. */
  private replaced?: 'restart' | 'crash';
  private readonly coalesceMs: number;

  constructor(private readonly o: RunTrackerOptions) {
    this.coalesceMs = o.coalesceMs ?? 250;
  }

  observeCell(c: CellObservation): void {
    if (this.replaced === 'crash') {
      // VS Code marks the last cell as running and writes the crash text into it:
      // not a run, and not stored, until a new kernel is seen
      return;
    }
    let run = this.open.get(c.cellId);
    if (
      !run &&
      c.executionOrder !== undefined &&
      c.success === undefined &&
      this.lastOrder.get(c.cellId) !== c.executionOrder
    ) {
      run = this.start(c.cellId, c.executionOrder);
    }
    if (!run) {
      if (c.outputs.length === 0 && (this.seenOutputs.get(c.cellId) ?? 0) > 0) {
        this.o.session.outputsCleared(c.cellId);
      }
      this.seenOutputs.set(c.cellId, c.outputs.length);
      return;
    }
    this.seenOutputs.set(c.cellId, c.outputs.length);
    if (!run.alive) {
      // recording was off when this run started: nothing of it is written
      if (c.success !== undefined) {
        this.open.delete(c.cellId);
      }
      return;
    }
    run.latest = c.outputs;
    if (c.success === undefined) {
      if (!run.timer) {
        run.timer = this.o.scheduler.setTimeout(() => {
          run!.timer = undefined;
          this.step(() => this.sendOutputs(run!));
        }, this.coalesceMs);
      }
      return;
    }
    run.timer?.();
    run.timer = undefined;
    this.open.delete(c.cellId);
    const status = c.success
      ? 'ok'
      : c.errorName === 'KeyboardInterrupt'
        ? 'interrupted'
        : 'error';
    this.step(async () => {
      try {
        await this.sendOutputs(run!);
      } finally {
        // a failed output conversion must not leave the run open
        if (run!.alive) {
          this.o.session.queue({
            type: 'run_finished',
            payload: { runId: run!.runId, status },
            cellId: run!.cellId,
            runId: run!.runId,
            atMs: this.o.clock.nowMs()
          });
        }
      }
    });
  }

  observeKernel(k: KernelObservation): void {
    const at = this.o.clock.nowMs();
    if (k.state === 'started') {
      if (this.kernelId !== undefined && !this.replaced) {
        return;
      }
      const wasCrash = this.replaced === 'crash';
      const first = this.kernelId === undefined;
      this.kernelId = this.o.ids.uuid();
      this.replaced = undefined;
      if (first || wasCrash) {
        const payload = {
          kernelId: this.kernelId,
          kernelName: k.kernelName,
          language: k.language
        };
        this.step(() =>
          this.o.session.queue({ type: 'kernel_started', payload, atMs: at })
        );
      }
      return;
    }
    if (this.kernelId === undefined || this.replaced) {
      return;
    }
    const reason = k.state === 'dead' ? 'crash' : 'requested';
    this.replaced = k.state === 'dead' ? 'crash' : 'restart';
    const kernelId = this.kernelId;
    for (const run of this.open.values()) {
      run.timer?.();
      run.timer = undefined;
      this.step(() => this.sendOutputs(run));
    }
    this.open.clear();
    this.lastOrder.clear();
    if (k.state === 'dead') {
      this.seenOutputs.clear();
    }
    this.step(() =>
      this.o.session.queue({
        type: 'kernel_restarted',
        payload: { kernelId, reason },
        atMs: at
      })
    );
  }

  /** Cancel the timers, send the outputs still waiting, and resolve when all steps are done. */
  close(): Promise<void> {
    for (const run of this.open.values()) {
      run.timer?.();
      run.timer = undefined;
      this.step(() => this.sendOutputs(run));
    }
    this.open.clear();
    return this.chain.then(() => undefined);
  }

  private start(modelCellId: string, order: number): Run {
    const run: Run = {
      runId: this.o.ids.uuid(),
      alive: true,
      latest: [],
      sent: []
    };
    this.open.set(modelCellId, run);
    this.lastOrder.set(modelCellId, order);
    const at = this.o.clock.nowMs();
    if (this.kernelId === undefined && this.o.kernelInfo) {
      this.observeKernel({ state: 'started', ...this.o.kernelInfo() });
    }
    this.o.session.beforeRun(modelCellId);
    // asked now, so a later edit cannot become this run's revision while earlier steps wait
    const revision = this.o.session.cellRevision(modelCellId);
    this.step(async () => {
      const cell = await revision;
      if (!cell) {
        run.alive = false;
        return;
      }
      run.cellId = cell.cellId;
      this.o.session.queue({
        type: 'run_started',
        payload: {
          runId: run.runId,
          cellId: cell.cellId,
          cellRevision: cell.revision,
          runCount: order,
          codeLink: 'by_time'
        },
        cellId: cell.cellId,
        runId: run.runId,
        atMs: at
      });
    });
    return run;
  }

  /** Send what changed in the run's output list since the last send. */
  private async sendOutputs(run: Run): Promise<void> {
    if (!run.alive) {
      return;
    }
    const next = run.latest.map(x => JSON.stringify(x));
    if (same(run.sent, next)) {
      return;
    }
    const cleared = next.length < run.sent.length;
    const from = cleared ? 0 : run.sent.length;
    const changed: number[] = [];
    next.forEach((s, i) => {
      if (cleared || i >= run.sent.length || s !== run.sent[i]) {
        changed.push(i);
      }
    });
    const res = await this.o.transport.prepareOutputs({
      explorationId: this.o.explorationId(),
      outputs: changed.map(i => run.latest[i])
    });
    const base = { runId: run.runId, cellId: run.cellId as string };
    const draft = (type: 'output_added' | 'output_replaced', i: number) => ({
      type,
      payload: { ...base, outputIndex: i, item: res.items[changed.indexOf(i)] },
      cellId: run.cellId,
      runId: run.runId,
      atMs: this.o.clock.nowMs()
    });
    if (cleared) {
      this.o.session.queue({
        type: 'outputs_cleared',
        payload: { ...base, wait: false },
        cellId: run.cellId,
        runId: run.runId,
        atMs: this.o.clock.nowMs()
      });
    }
    for (const i of changed) {
      this.o.session.queue(
        draft(cleared || i >= from ? 'output_added' : 'output_replaced', i)
      );
    }
    run.sent = next;
  }

  /** Steps run one after another, in the order seen. */
  private step(fn: () => unknown): void {
    this.chain = this.chain.then(fn).catch(() => undefined);
  }
}
