import type { IngestRequest, RecorderEvent } from 'blaym-jupyter-types';
import { applyPolicy, Outbox, OutboxReport, utf8Length } from '../../recording';
import {
  accept,
  FakeNotebook,
  FakeTime,
  FakeTransport,
  Harness,
  POLICY,
  refuse,
  settle
} from './generate';

let n = 0;
const ev = (type: string): RecorderEvent =>
  ({
    eventId: `r${++n}`,
    type,
    payload: { cellId: 'c' }
  }) as unknown as RecorderEvent;
const sentIds = (rs: IngestRequest[]): string[] =>
  rs.reduce((a: string[], r) => a.concat(idsOf(r)), []);
const idsOf = (r: IngestRequest) => r.events.map(e => (e as any).eventId);

function make(snapshotter?: () => Promise<RecorderEvent>) {
  const time = new FakeTime();
  const transport = new FakeTransport();
  const reports: OutboxReport[] = [];
  const snapshots: RecorderEvent[] = [];
  const outbox = new Outbox({
    transport,
    scheduler: time,
    clock: time,
    explorationId: 'nb1',
    editor: 'jupyterlab',
    instanceId: 'tab1',
    snapshotter:
      snapshotter ??
      (async () => {
        const s = ev('notebook_snapshot');
        snapshots.push(s);
        return s;
      }),
    onReport: r => reports.push(r)
  });
  return { time, transport, reports, snapshots, outbox };
}

describe('review: outbox adversarial', () => {
  // The pump may send an event enqueued during the build ahead of the snapshot (harmless). The
  // loss happens when the pump is busy: sendSnapshot filters every queued content event, including
  // ones enqueued after the snapshotter captured the model.
  test('a content event enqueued while the snapshot is being built (pump busy) is kept and sent after it', async () => {
    let release!: () => void;
    const gate = new Promise<void>(r => (release = r));
    const snap = ev('notebook_snapshot');
    const { outbox, transport, time } = make(async () => {
      await gate;
      return snap;
    });
    let hang!: () => void;
    transport.ingestHandler = (req, call) =>
      call === 0
        ? new Promise(r => (hang = () => r(accept(req))))
        : Promise.resolve(accept(req));
    outbox.enqueue(ev('cell_inserted')); // in flight, unanswered
    await settle();
    outbox.requestResync('start');
    time.advance(0);
    await settle();
    const late = ev('cell_source_changed');
    outbox.enqueue(late);
    release();
    await settle();
    hang();
    await settle(50);
    const sent = sentIds(transport.requests);
    expect(sent).toContain((late as any).eventId);
    expect(sent.indexOf((snap as any).eventId)).toBeLessThan(
      sent.indexOf((late as any).eventId)
    );
  });

  test('partial acceptance in one response: accepted gone, refused dropped with one resync, untouched kept', async () => {
    const { outbox, transport, time, snapshots } = make();
    const first = ev('cell_inserted');
    const b = ev('cell_source_changed'),
      c = ev('recording_gap_started'),
      d = ev('cell_deleted');
    let held!: () => void;
    transport.ingestHandler = (req, call) => {
      if (call === 0) return new Promise(r => (held = () => r(accept(req))));
      if (call === 1) {
        const ok = accept(req).outcomes;
        return Promise.resolve({
          apiVersion: 1,
          outcomes: [
            refuse({ ...req, events: [req.events[0]] }, 'invalid_event')
              .outcomes[0],
            ok[1],
            refuse({ ...req, events: [req.events[2]] }, 'conflict').outcomes[0]
          ]
        } as any);
      }
      return Promise.resolve(accept(req));
    };
    outbox.enqueue(first);
    await settle();
    [b, c, d].forEach(e => outbox.enqueue(e));
    held();
    await settle();
    time.advance(5000);
    await settle(50);
    const sent = sentIds(transport.requests);
    const count = (e: RecorderEvent) =>
      sent.filter((x: string) => x === (e as any).eventId).length;
    expect([count(b), count(c), count(d)]).toEqual([1, 1, 1]);
    expect(snapshots).toHaveLength(1);
    expect(outbox.length).toBe(0);
  });

  test('a persistently refused snapshot does not spin: at most one build per 5 s', async () => {
    const { outbox, transport, time, snapshots } = make();
    transport.ingestHandler = req =>
      Promise.resolve(refuse(req, 'invalid_event'));
    outbox.requestResync('start');
    for (let i = 0; i < 60; i++) {
      time.advance(1000);
      await settle(20);
    }
    expect(snapshots.length).toBeLessThanOrEqual(13);
    expect(transport.requests.length).toBeLessThanOrEqual(13);
  });

  test('not_active_recorder pauses, sends nothing more, and resume sends the rest once', async () => {
    const { outbox, transport } = make();
    transport.ingestHandler = (req, call) =>
      Promise.resolve(
        call === 0 ? refuse(req, 'not_active_recorder') : accept(req)
      );
    const a = ev('cell_inserted');
    outbox.enqueue(a);
    await settle();
    outbox.enqueue(ev('cell_inserted'));
    await settle(30);
    expect(outbox.isPaused).toBe(true);
    expect(transport.requests).toHaveLength(1);
  });

  test('close stops timers: no request after close', async () => {
    const { outbox, transport, time } = make();
    transport.ingestHandler = () =>
      Promise.reject(Object.assign(new Error('x'), {}));
    outbox.enqueue(ev('cell_inserted'));
    await settle();
    const calls = transport.calls;
    outbox.close();
    time.advance(60000);
    await settle(30);
    expect(transport.calls).toBe(calls);
  });
});

describe('review: metadata', () => {
  test('non-ASCII: size counts UTF-8 bytes of unescaped compact JSON', () => {
    const limit = POLICY.maxValueBytes;
    // each "é" is 2 bytes: chars well below the limit, bytes above it
    const s = 'é'.repeat(Math.floor(limit / 2));
    const out = applyPolicy({ k: s }, 'notebook', POLICY);
    expect(out.dropped).toEqual(['k']);
    expect((out.metadata.k as any).bytes).toBe(utf8Length(JSON.stringify(s)));
    expect((out.metadata.k as any).bytes).toBe(2 * s.length + 2);
    // lone surrogate counts 3 bytes (Python would refuse it; documented difference), pairs 4
    expect(utf8Length('😀')).toBe(4);
    // an astral char is escaped by neither JSON.stringify nor Python ensure_ascii=False
    expect(utf8Length(JSON.stringify({ a: '😀' }))).toBe(12);
  });

  test('odd values: null, arrays, numbers, nested objects survive; undefined metadata gives {}', () => {
    const m = { a: null, b: [1, { c: 'é' }], d: 1.5, e: { f: {} } };
    expect(applyPolicy(m, 'cell', POLICY).metadata).toEqual(m);
    expect(applyPolicy(undefined, 'cell', POLICY).metadata).toEqual({});
  });
});

describe('review: metadata policy cannot be bypassed in reconcile/builder', () => {
  test('a change only in dropped keys (trusted, execution timing) emits no event; a real key does', async () => {
    const nb = new FakeNotebook();
    nb.insert(0, 'code', 'a');
    const h = new Harness(nb);
    await h.snapshot();
    nb.list[0].metadata = {
      trusted: true,
      execution: { 'iopub.execute_input': 'x' }
    };
    expect(
      (await h.change()).filter(e => e.type === 'cell_metadata_changed')
    ).toHaveLength(0);
    nb.list[0].metadata = {
      trusted: false,
      execution: { 'iopub.execute_input': 'y' },
      tags: ['t']
    };
    const ev2 = await h.change();
    const m = ev2.filter(e => e.type === 'cell_metadata_changed') as any[];
    expect(m).toHaveLength(1);
    expect(m[0].payload.metadata).toEqual({ tags: ['t'] });
  });
});
