import * as fc from 'fast-check';
import type {
  CellType,
  IngestRequest,
  IngestResponse,
  NotebookStatus,
  OutputItem,
  RecorderEvent
} from 'blaym-jupyter-types';
import {
  AuthorMarker,
  Cancel,
  CellIdentity,
  CellView,
  Clock,
  EventBuilder,
  EventDraft,
  Ids,
  MetadataPolicy,
  NotebookView,
  Reconciler,
  Scheduler,
  Transport,
  TypingGrouper
} from '../../recording';

declare const require: (name: string) => any;
const { readFileSync } = require('fs');
const { join } = require('path');
declare const __dirname: string;

export const FIXTURES: string = join(__dirname, '..', '..', '..', 'fixtures', 'recorder');
export const POLICY: MetadataPolicy = JSON.parse(
  readFileSync(join(FIXTURES, 'metadata-policy.json'), 'utf8')
);

// ---------------------------------------------------------------------------
// Fake time, ids, transport
// ---------------------------------------------------------------------------

export class FakeTime implements Clock, Scheduler {
  t = 1_000_000;
  private timers: Array<{ at: number; seq: number; fn: () => void; live: boolean }> = [];
  private seq = 0;

  nowMs(): number {
    return this.t;
  }

  setTimeout(fn: () => void, ms: number): Cancel {
    const timer = { at: this.t + ms, seq: this.seq++, fn, live: true };
    this.timers.push(timer);
    return () => {
      timer.live = false;
    };
  }

  /** Advance time, running due timers in order (timers they set run too). */
  advance(ms: number): void {
    const end = this.t + ms;
    for (;;) {
      const due = this.timers
        .filter(x => x.live && x.at <= end)
        .sort((a, b) => a.at - b.at || a.seq - b.seq)[0];
      if (!due) {
        break;
      }
      due.live = false;
      this.t = Math.max(this.t, due.at);
      due.fn();
    }
    this.t = end;
    this.timers = this.timers.filter(x => x.live);
  }

  pending(): number {
    return this.timers.filter(x => x.live).length;
  }
}

/** Lets promises settle (the fake transport answers in microtasks). */
export async function settle(times = 20): Promise<void> {
  for (let i = 0; i < times; i++) {
    await Promise.resolve();
  }
}

export class SeqIds implements Ids {
  n = 0;
  constructor(private readonly prefix = 'id') {}
  uuid(): string {
    return `${this.prefix}-${++this.n}`;
  }
}

export function fakeItem(o: Record<string, unknown>, i: number): OutputItem {
  return {
    outputType: (o.output_type as OutputItem['outputType']) ?? 'stream',
    mimeTypes: ['text/plain'],
    contentHash: `h${i}`,
    preview: JSON.stringify(o).slice(0, 40),
    truncated: false,
    sizeBytes: 1
  };
}

export function status(over: Partial<NotebookStatus> = {}): NotebookStatus {
  return {
    apiVersion: 1,
    notebookId: 'nb1',
    explorationId: 'nb1',
    recording: true,
    active: true,
    needsRegister: false,
    needsResync: false,
    runsOpen: 0,
    openGaps: [],
    cellRevisions: {},
    notices: [],
    ...over
  };
}

export class FakeTransport implements Transport {
  prepared: Array<Record<string, unknown>[]> = [];
  requests: IngestRequest[] = [];
  closed: Array<[string, string]> = [];
  keepalive: IngestRequest[] = [];
  registers = 0;
  statuses = 0;
  /** Decides each ingest call: reject with an error, or answer per event. */
  ingestHandler: (req: IngestRequest, call: number) => Promise<IngestResponse> = (req) =>
    Promise.resolve(accept(req));
  statusHandler: () => Promise<NotebookStatus> = () => Promise.resolve(status());
  registerHandler: () => Promise<NotebookStatus> = () => Promise.resolve(status());
  calls = 0;

  ingest(req: IngestRequest): Promise<IngestResponse> {
    this.requests.push(req);
    return this.ingestHandler(req, this.calls++);
  }
  prepareOutputs(req: { explorationId: string; outputs: Record<string, unknown>[] }) {
    this.prepared.push(req.outputs);
    return Promise.resolve({ apiVersion: 1 as const, items: req.outputs.map(fakeItem) });
  }
  register() {
    this.registers++;
    return this.registerHandler();
  }
  status() {
    this.statuses++;
    return this.statusHandler();
  }
  close(nb: string, inst: string): void {
    this.closed.push([nb, inst]);
  }
  ingestKeepalive(req: IngestRequest): void {
    this.keepalive.push(req);
  }
}

export function accept(req: IngestRequest, branchId = 'b1'): IngestResponse {
  return {
    apiVersion: 1,
    outcomes: req.events.map((e, i) => ({
      status: 'accepted' as const,
      eventId: (e as { eventId: string }).eventId,
      branchId,
      tick: i + 1,
      duplicate: false
    }))
  };
}

export function refuse(
  req: IngestRequest,
  reason: string,
  only?: (e: RecorderEvent, i: number) => boolean
): IngestResponse {
  return {
    apiVersion: 1,
    outcomes: req.events.map((e, i) =>
      !only || only(e, i)
        ? {
            status: 'refused' as const,
            eventId: (e as { eventId: string }).eventId,
            reason: reason as never,
            detail: reason
          }
        : {
            status: 'accepted' as const,
            eventId: (e as { eventId: string }).eventId,
            branchId: 'b1',
            tick: i + 1,
            duplicate: false
          }
    )
  };
}

// ---------------------------------------------------------------------------
// The fake notebook (the editor model)
// ---------------------------------------------------------------------------

export class FakeNotebook implements NotebookView {
  list: CellView[] = [];
  nbMetadata: Record<string, unknown> = {};
  cellIdsStable = true;
  private n = 0;

  cells(): CellView[] {
    return this.list.map(c => ({
      ...c,
      metadata: JSON.parse(JSON.stringify(c.metadata)),
      outputs: c.outputs.slice()
    }));
  }
  notebookMetadata(): Record<string, unknown> {
    return JSON.parse(JSON.stringify(this.nbMetadata));
  }
  freshId(): string {
    return `m${++this.n}-${Math.floor(Math.random() * 1e9).toString(36)}`;
  }
  insert(index: number, type: CellType, source: string, metadata: Record<string, unknown> = {}): string {
    const id = this.freshId();
    this.list.splice(Math.min(index, this.list.length), 0, { id, type, source, metadata, outputs: [] });
    return id;
  }
  delete(index: number): void {
    this.list.splice(index, 1);
  }
  move(from: number, to: number): void {
    const [c] = this.list.splice(from, 1);
    this.list.splice(to, 0, c);
  }
  /** Replace every id (a reload or revert). */
  reload(): void {
    this.list = this.list.map(c => ({ ...c, id: this.freshId() }));
  }
}

// Operations on the fake notebook, generated by fast-check. Indexes wrap with modulo.
export type Op =
  | { op: 'insert'; at: number; type: CellType; source: string }
  | { op: 'delete'; at: number }
  | { op: 'move'; from: number; to: number }
  | { op: 'type'; at: number; type: CellType }
  | { op: 'edit'; at: number; source: string }
  | { op: 'meta'; at: number; tags: string[] }
  | { op: 'nbmeta'; key: string; value: number };

const typeArb = fc.constantFrom<CellType>('code', 'markdown', 'raw');
const textArb = fc.string({ maxLength: 12 });

export const opArb: fc.Arbitrary<Op> = fc.oneof(
  { weight: 3, arbitrary: fc.record({ op: fc.constant('insert' as const), at: fc.nat(30), type: typeArb, source: textArb }) },
  { weight: 2, arbitrary: fc.record({ op: fc.constant('delete' as const), at: fc.nat(30) }) },
  { weight: 3, arbitrary: fc.record({ op: fc.constant('move' as const), from: fc.nat(30), to: fc.nat(30) }) },
  { weight: 2, arbitrary: fc.record({ op: fc.constant('type' as const), at: fc.nat(30), type: typeArb }) },
  { weight: 5, arbitrary: fc.record({ op: fc.constant('edit' as const), at: fc.nat(30), source: textArb }) },
  { weight: 2, arbitrary: fc.record({ op: fc.constant('meta' as const), at: fc.nat(30), tags: fc.array(fc.constantFrom('a', 'b', 'parameters'), { maxLength: 2 }) }) },
  { weight: 1, arbitrary: fc.record({ op: fc.constant('nbmeta' as const), key: fc.constantFrom('x', 'y', 'kernelspec'), value: fc.nat(5) }) }
);

/** A step is one or more operations between two reconciles. */
export const stepsArb = fc.array(fc.array(opArb, { minLength: 1, maxLength: 4 }), { minLength: 1, maxLength: 25 });

export function applyOp(nb: FakeNotebook, o: Op): void {
  const n = nb.list.length;
  switch (o.op) {
    case 'insert':
      nb.insert(n === 0 ? 0 : o.at % (n + 1), o.type, o.source);
      break;
    case 'delete':
      if (n > 0) nb.delete(o.at % n);
      break;
    case 'move':
      if (n > 1) nb.move(o.from % n, o.to % n);
      break;
    case 'type':
      if (n > 0) nb.list[o.at % n].type = o.type;
      break;
    case 'edit':
      if (n > 0) nb.list[o.at % n].source = o.source;
      break;
    case 'meta':
      if (n > 0) nb.list[o.at % n].metadata = o.tags.length ? { tags: o.tags } : {};
      break;
    case 'nbmeta':
      nb.nbMetadata = { ...nb.nbMetadata, [o.key]: o.value };
      break;
  }
}

// ---------------------------------------------------------------------------
// An independent applier of recorded events (never uses src/recording logic)
// ---------------------------------------------------------------------------

export interface ModelCell {
  id: string;
  type: string;
  code: string;
  metadata: Record<string, unknown>;
  revision: number;
}

export class Applier {
  cells: ModelCell[] = [];
  nb: Record<string, unknown> = {};
  /** Every violation found while applying (revision jumps and so on). */
  problems: string[] = [];

  apply(e: RecorderEvent): void {
    const p = (e as any).payload;
    switch (e.type) {
      case 'notebook_snapshot':
        this.cells = p.cells.map((c: any) => {
          const old = this.cells.find(x => x.id === c.cellId);
          if (old && c.revision < old.revision) {
            this.problems.push(`snapshot lowered revision of ${c.cellId}`);
          }
          return { id: c.cellId, type: c.cellType, code: c.code, metadata: c.metadata, revision: c.revision };
        });
        this.nb = p.notebookMetadata;
        break;
      case 'cell_inserted':
        if (this.cells.some(c => c.id === p.cellId)) this.problems.push(`duplicate id ${p.cellId}`);
        if (p.index > this.cells.length) this.problems.push('insert index out of range');
        this.cells.splice(p.index, 0, { id: p.cellId, type: p.cellType, code: p.code, metadata: p.metadata ?? {}, revision: p.revision });
        break;
      case 'cell_deleted':
        this.cells = this.cells.filter(c => c.id !== p.cellId);
        break;
      case 'cell_moved': {
        const i = this.cells.findIndex(c => c.id === p.cellId);
        if (i !== p.fromIndex) this.problems.push(`move from ${p.fromIndex} but cell at ${i}`);
        const [c] = this.cells.splice(i, 1);
        this.cells.splice(p.toIndex, 0, c);
        break;
      }
      case 'cell_type_changed':
        this.find(p.cellId).type = p.cellType;
        break;
      case 'cell_source_changed': {
        const c = this.find(p.cellId);
        if (p.revision !== c.revision + 1) this.problems.push(`revision ${p.revision} after ${c.revision}`);
        c.revision = p.revision;
        c.code = p.code;
        break;
      }
      case 'cell_metadata_changed':
        this.find(p.cellId).metadata = p.metadata;
        break;
      case 'notebook_metadata_changed':
        this.nb = p.metadata;
        break;
      default:
        break;
    }
  }

  private find(id: string): ModelCell {
    const c = this.cells.find(x => x.id === id);
    if (!c) throw new Error(`event for unknown cell ${id}`);
    return c;
  }
}

/** The notebook as the policy leaves it, for comparison with the applier. */
export function expectedModel(nb: FakeNotebook, identity: CellIdentity) {
  return {
    cells: nb.list.map(c => ({
      id: identity.blaymId(c.id),
      type: c.type,
      code: c.source,
      metadata: c.metadata
    })),
    nb: Object.keys(nb.nbMetadata)
      .filter(k => k !== 'kernelspec' && k !== 'language_info')
      .reduce((o: Record<string, unknown>, k) => ({ ...o, [k]: nb.nbMetadata[k] }), {})
  };
}

export function appliedModel(a: Applier) {
  return {
    cells: a.cells.map(c => ({ id: c.id, type: c.type, code: c.code, metadata: c.metadata })),
    nb: a.nb
  };
}

// ---------------------------------------------------------------------------
// Harness: the wiring an adapter will do, on fakes
// ---------------------------------------------------------------------------

export class Harness {
  time = new FakeTime();
  ids = new SeqIds('ev');
  transport = new FakeTransport();
  nb: FakeNotebook;
  identity: CellIdentity;
  reconciler = new Reconciler(POLICY);
  builder: EventBuilder;
  grouper: TypingGrouper;
  marker?: AuthorMarker;
  events: RecorderEvent[] = [];
  steps: RecorderEvent[][] = [];
  running = new Set<string>();
  resyncs = 0;
  private pending: Array<Promise<RecorderEvent>> = [];

  constructor(nb: FakeNotebook = new FakeNotebook(), opts: { marker?: (h: Harness) => AuthorMarker } = {}) {
    this.nb = nb;
    this.identity = new CellIdentity(nb.cellIdsStable, new SeqIds('blaym'));
    this.builder = new EventBuilder({
      clock: this.time,
      ids: this.ids,
      transport: this.transport,
      explorationId: 'nb1',
      editor: 'jupyterlab',
      originId: 'tab1',
      policy: POLICY
    });
    this.grouper = new TypingGrouper({
      scheduler: this.time,
      clock: this.time,
      emit: (d, a) => this.queue(d, a),
      mark: (id, from, to) => (this.marker ? this.marker.mark(id, from, to) : { hint: 'unknown' })
    });
    if (opts.marker) {
      this.marker = opts.marker(this);
    }
  }

  private queue(d: EventDraft, a?: { hint: any; confidence?: any }): void {
    this.pending.push(this.builder.build(d, a));
  }

  /** The first snapshot (or a resync): built from the model now. */
  async snapshot(): Promise<RecorderEvent> {
    const v = this.view();
    const ev = await this.builder.snapshot({
      path: 'a.ipynb',
      notebookMetadata: this.nb.notebookMetadata(),
      kernelName: 'python3',
      language: 'python',
      cells: v
    });
    this.reconciler.reset(this.nb, this.identity);
    v.forEach(c => this.grouper.baseline(c.id, c.code));
    this.events.push(ev);
    return ev;
  }

  private view() {
    return this.nb.cells().map(c => ({
      id: this.identity.blaymId(c.id),
      modelId: c.id,
      type: c.type,
      code: c.source,
      metadata: c.metadata,
      outputs: c.outputs
    }));
  }

  /** Reconcile after a change; returns the events this step produced (typing still pending is not included). */
  async change(): Promise<RecorderEvent[]> {
    const before = this.events.length;
    const r = this.reconciler.reconcile(this.nb, this.identity, {
      isRunning: id => this.running.has(id)
    });
    if (r.kind === 'resync') {
      this.resyncs++;
      await this.settleQueue();
      await this.snapshot();
    } else {
      r.edits.forEach(e => this.grouper.edit(e.cellId, e.code));
      r.flushBefore.forEach(id => this.grouper.flushCell(id));
      for (const d of r.drafts) {
        this.queue(d, { hint: 'unknown' });
        if (d.type === 'cell_inserted') {
          this.grouper.baseline(d.cellId as string, String(d.payload.code));
        }
        if (d.type === 'cell_deleted') {
          this.grouper.forget(d.cellId as string);
        }
      }
    }
    await this.settleQueue();
    const mine = this.events.slice(before);
    this.steps.push(mine);
    return mine;
  }

  /** Flush typing and collect everything built so far. */
  async finish(): Promise<void> {
    this.grouper.flushAll();
    await this.settleQueue();
  }

  private async settleQueue(): Promise<void> {
    const p = this.pending;
    this.pending = [];
    for (const x of p) {
      this.events.push(await x);
    }
  }
}
