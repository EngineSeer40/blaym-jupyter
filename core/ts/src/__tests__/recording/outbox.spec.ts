import * as fc from 'fast-check';
import type { IngestRequest, RecorderEvent } from 'blaym-notebook-types';
import { Outbox, OutboxReport, TransportError } from '../../recording';
import { accept, FakeTime, FakeTransport, refuse, settle } from './generate';

let n = 0;
const ev = (type: string, extra: Record<string, unknown> = {}): RecorderEvent =>
  ({
    eventId: `e${++n}`,
    type,
    payload: { cellId: 'c', ...extra }
  }) as unknown as RecorderEvent;
const content = () => ev('cell_source_changed', { code: 'x' });
const snapEvent = () => ev('notebook_snapshot', { cells: [] });
const ids = (req: IngestRequest) => req.events.map(e => (e as any).eventId);

function make(over: Partial<ConstructorParameters<typeof Outbox>[0]> = {}) {
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
    snapshotter: async () => {
      const s = snapEvent();
      snapshots.push(s);
      return s;
    },
    onReport: r => reports.push(r),
    ...over
  });
  return { time, transport, reports, snapshots, outbox };
}
const tick = async (time: FakeTime, ms: number) => {
  time.advance(ms);
  await settle();
};

describe('Outbox batching and retry', () => {
  test('batches hold at most 50 events', async () => {
    const { transport, outbox, time } = make();
    for (let i = 0; i < 120; i++) outbox.enqueue(content());
    await tick(time, 0);
    await settle(200);
    expect(transport.requests.map(r => r.events.length)).toEqual(
      [1, 50, 50, 19].slice(0, transport.requests.length)
    );
    expect(transport.requests.every(r => r.events.length <= 50)).toBe(true);
    expect(transport.requests.reduce((s, r) => s + r.events.length, 0)).toBe(
      120
    );
    expect(outbox.length).toBe(0);
  });

  test('network errors retry at 0.5 s doubling to 8 s, then recover', async () => {
    const { transport, outbox, time, reports } = make();
    transport.ingestHandler = (req, call) =>
      call < 7
        ? Promise.reject(new TransportError('offline', 'network'))
        : Promise.resolve(accept(req));
    const e = content();
    outbox.enqueue(e);
    await settle();
    const delays: number[] = [];
    for (let i = 0; i < 7; i++) {
      const r = reports.filter(x => x.kind === 'retrying');
      delays.push((r[r.length - 1] as any).delayMs);
      await tick(time, (r[r.length - 1] as any).delayMs);
    }
    expect(delays).toEqual([500, 1000, 2000, 4000, 8000, 8000, 8000]);
    expect(outbox.length).toBe(0);
    expect(
      transport.requests.every(r => ids(r)[0] === (e as any).eventId)
    ).toBe(true);
  });
});

describe('Outbox response table', () => {});

describe('Outbox resync', () => {
  test('requests coalesce: one snapshot, and at most one build every 5 s', async () => {
    const { outbox, snapshots, time, transport } = make();
    transport.ingestHandler = req =>
      Promise.resolve(
        req.events.some(e => e.type === 'notebook_snapshot')
          ? accept(req)
          : refuse(req, 'invalid_event')
      );
    outbox.requestResync('a');
    outbox.requestResync('b');
    await tick(time, 0);
    await settle(30);
    expect(snapshots).toHaveLength(1);
    // a storm of refused events right after: the next snapshot waits for the 5 s window
    for (let i = 0; i < 20; i++) outbox.enqueue(content());
    await settle(100);
    await tick(time, 4000);
    await settle(50);
    expect(snapshots).toHaveLength(1);
    await tick(time, 1000);
    await settle(50);
    expect(snapshots).toHaveLength(2);
  });

  test('memory bound: beyond 5,000 events the oldest content events go and a resync is wanted; other events stay', async () => {
    const { outbox, transport, reports } = make();
    transport.ingestHandler = () =>
      Promise.reject(new TransportError('offline', 'network'));
    const gap = ev('recording_gap_started');
    outbox.enqueue(gap);
    for (let i = 0; i < 5200; i++) outbox.enqueue(content());
    expect(outbox.length).toBeLessThanOrEqual(5000);
    expect(outbox.needsResync).toBe(true);
    expect(reports.some(r => r.kind === 'dropped')).toBe(true);
    expect((outbox as any).queue[0]).toBe(gap);
  });
});

describe('Outbox persistent snapshot refusal', () => {
  test('after 5 refused snapshots it pauses with snapshot_refused; resume or requestResync restarts it', async () => {
    const { outbox, transport, snapshots, reports, time } = make();
    let ok = false;
    transport.ingestHandler = req =>
      Promise.resolve(ok ? accept(req) : refuse(req, 'invalid_event'));
    outbox.requestResync('start');
    for (let i = 0; i < 12; i++) await tick(time, 5000);
    expect(snapshots).toHaveLength(5);
    expect(outbox.isPaused).toBe(true);
    expect(
      reports.filter(
        r => r.kind === 'paused' && r.reason === 'snapshot_refused'
      )
    ).toHaveLength(1);
    ok = true;
    outbox.resume();
    await tick(time, 5000);
    await settle(50);
    expect(snapshots).toHaveLength(6);
    expect(outbox.length).toBe(0);
  });
});

describe('Outbox property: delivery failures never lose or reorder events', () => {
  test('every event arrives once, in order, whatever the failures', async () => {
    const action = fc.oneof(
      fc.constant('enqueue' as const),
      fc.constant('fail' as const),
      fc.constant('heal' as const),
      fc.integer({ min: 0, max: 9000 })
    );
    await fc.assert(
      fc.asyncProperty(fc.array(action, { maxLength: 60 }), async actions => {
        const { outbox, transport, time } = make();
        let failing = false;
        const received: string[] = [];
        transport.ingestHandler = req => {
          if (failing)
            return Promise.reject(new TransportError('offline', 'network'));
          received.push(...ids(req));
          return Promise.resolve(accept(req));
        };
        const sent: string[] = [];
        for (const a of actions) {
          if (a === 'enqueue') {
            const e = content();
            sent.push((e as any).eventId);
            outbox.enqueue(e);
          } else if (a === 'fail') failing = true;
          else if (a === 'heal') failing = false;
          else await tick(time, a);
          await settle();
        }
        failing = false;
        for (let i = 0; i < 12; i++) await tick(time, 8000);
        expect(received).toEqual(sent);
        expect(outbox.length).toBe(0);
      }),
      { numRuns: 100 }
    );
  });
});

describe('Outbox refusal table', () => {
  test('each ingest refusal does what the table says', async () => {
    const answer = (
      reason: string,
      req: IngestRequest
    ): Promise<ReturnType<typeof accept>> => {
      const isSnapshot = req.events[0].type === 'notebook_snapshot';
      if (reason === 'unknown_branch')
        return Promise.resolve(
          req.targetBranchId ? refuse(req, reason) : accept(req)
        );
      return Promise.resolve(isSnapshot ? accept(req) : refuse(req, reason));
    };
    for (const reason of ['unauthorized', 'not_active_recorder']) {
      const { transport, outbox, reports } = make();
      transport.ingestHandler = req => Promise.resolve(refuse(req, reason));
      outbox.enqueue(content());
      await settle();
      outbox.enqueue(content());
      await settle();
      expect(outbox.isPaused).toBe(true);
      expect(
        reports.some(r => r.kind === 'paused' && r.reason === reason)
      ).toBe(true);
      expect(transport.requests).toHaveLength(1);
      transport.ingestHandler = req => Promise.resolve(accept(req));
      outbox.resume();
      await settle();
      expect(outbox.length).toBe(0);
    }
    for (const reason of [
      'invalid_event',
      'conflict',
      'unsupported_schema_version',
      'missing_blob',
      'unknown_branch',
      'bad_first_event'
    ]) {
      const { transport, outbox, reports, snapshots, time } = make();
      if (reason === 'unknown_branch') outbox.retarget('gone');
      transport.ingestHandler = req => answer(reason, req);
      outbox.enqueue(content());
      outbox.enqueue(content());
      await settle();
      await tick(time, 0);
      await settle(50);
      expect(snapshots).toHaveLength(1);
      expect(outbox.length).toBe(0);
      expect(outbox.isPaused).toBe(false);
      const last = transport.requests[transport.requests.length - 1];
      if (reason === 'unknown_branch') {
        expect(transport.requests[0].targetBranchId).toBe('gone');
        expect(transport.requests[1].targetBranchId).toBeUndefined();
      } else if (reason === 'bad_first_event') {
        expect(last.events[0].type).toBe('notebook_snapshot');
      } else {
        expect(
          reports.some(r => r.kind === 'refused' && r.reason === reason)
        ).toBe(true);
      }
    }
  });

  test('a partly accepted response: accepted leave, refused drop with one resync, the rest keep their order', async () => {
    const { outbox, transport, time, snapshots } = make();
    const first = ev('cell_inserted');
    const [b, c, d] = [
      ev('cell_source_changed'),
      ev('recording_gap_started'),
      ev('cell_deleted')
    ];
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
    const sent = ([] as string[]).concat(...transport.requests.map(ids));
    const count = (e: RecorderEvent) =>
      sent.filter((x: string) => x === (e as any).eventId).length;
    expect([b, c, d].map(count)).toEqual([1, 1, 1]);
    expect(sent.indexOf((b as any).eventId)).toBeLessThan(
      sent.indexOf((c as any).eventId)
    );
    expect(snapshots).toHaveLength(1);
    expect(outbox.length).toBe(0);
  });
});
