import * as fc from 'fast-check';
import type {
  IngestRequest,
  IngestResponse,
  RecorderEvent
} from 'blaym-jupyter-types';
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

  test('batches hold at most 200 KB', async () => {
    const { transport, outbox } = make();
    let release!: (r: IngestResponse) => void;
    transport.ingestHandler = req =>
      transport.calls === 1
        ? new Promise(r => (release = r))
        : Promise.resolve(accept(req));
    outbox.enqueue(content());
    await settle();
    for (let i = 0; i < 10; i++)
      outbox.enqueue(
        ev('cell_source_changed', { code: 'z'.repeat(60 * 1024) })
      );
    release(accept(transport.requests[0]));
    await settle(200);
    const rest = transport.requests.slice(1);
    expect(
      rest.every(r => JSON.stringify(r.events).length <= 200 * 1024 + 2000)
    ).toBe(true);
    expect(rest.map(r => r.events.length)).toEqual([3, 3, 3, 1]);
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

  test('a refusal is never retried', async () => {
    const { transport, outbox, time } = make();
    transport.ingestHandler = req =>
      Promise.resolve(refuse(req, 'not_active_recorder'));
    outbox.enqueue(content());
    await settle();
    await tick(time, 120_000);
    expect(transport.requests).toHaveLength(1);
  });

  test('duplicate answers are accepted and removed', async () => {
    const { transport, outbox } = make();
    transport.ingestHandler = req => {
      const r = accept(req);
      (r.outcomes[0] as any).duplicate = true;
      return Promise.resolve(r);
    };
    outbox.enqueue(content());
    await settle();
    expect(outbox.length).toBe(0);
  });
});

describe('Outbox response table', () => {
  test('accepted records the branch and later requests target it (S5-P)', async () => {
    const branches: string[] = [];
    const { transport, outbox } = make({ onBranch: b => branches.push(b) });
    transport.ingestHandler = req => Promise.resolve(accept(req, 'bX'));
    outbox.enqueue(content());
    await settle();
    outbox.enqueue(content());
    await settle();
    expect(branches).toEqual(['bX', 'bX']);
    expect(transport.requests[0].targetBranchId).toBeUndefined();
    expect(transport.requests[1].targetBranchId).toBe('bX');
  });

  test.each(['not_active_recorder', 'unauthorized'])(
    '%s pauses and reports; resume sends again',
    async reason => {
      const { transport, outbox, reports } = make();
      let refuseNow = true;
      transport.ingestHandler = req =>
        Promise.resolve(refuseNow ? refuse(req, reason) : accept(req));
      outbox.enqueue(content());
      await settle();
      expect(outbox.isPaused).toBe(true);
      expect(
        reports.some(r => r.kind === 'paused' && r.reason === reason)
      ).toBe(true);
      outbox.enqueue(content());
      await settle();
      expect(transport.requests).toHaveLength(1);
      refuseNow = false;
      outbox.resume();
      await settle();
      expect(outbox.length).toBe(0);
    }
  );

  test.each(['invalid_event', 'conflict', 'unsupported_schema_version'])(
    '%s drops the event, reports and sends one snapshot',
    async reason => {
      const { transport, outbox, snapshots, reports, time } = make();
      transport.ingestHandler = req =>
        Promise.resolve(
          req.events.some(e => e.type === 'notebook_snapshot')
            ? accept(req)
            : refuse(req, reason)
        );
      outbox.enqueue(content());
      outbox.enqueue(content());
      await settle();
      await tick(time, 0);
      await settle(50);
      expect(snapshots).toHaveLength(1);
      expect(
        reports.some(r => r.kind === 'refused' && r.reason === reason)
      ).toBe(true);
      expect(outbox.length).toBe(0);
      expect(outbox.needsResync).toBe(false);
    }
  );

  test('bad_first_event sends a snapshot in place of the content', async () => {
    const { transport, outbox, snapshots, time } = make();
    transport.ingestHandler = req =>
      Promise.resolve(
        req.events[0].type === 'notebook_snapshot'
          ? accept(req)
          : refuse(req, 'bad_first_event')
      );
    outbox.enqueue(content());
    await settle();
    await tick(time, 0);
    await settle(50);
    expect(snapshots).toHaveLength(1);
    expect(
      transport.requests[transport.requests.length - 1].events[0].type
    ).toBe('notebook_snapshot');
  });

  test('unknown_branch drops the target branch and resyncs', async () => {
    const { transport, outbox, snapshots, time } = make();
    outbox.retarget('gone');
    transport.ingestHandler = req =>
      Promise.resolve(
        req.targetBranchId ? refuse(req, 'unknown_branch') : accept(req)
      );
    outbox.enqueue(content());
    await settle();
    await tick(time, 0);
    await settle(50);
    expect(transport.requests[0].targetBranchId).toBe('gone');
    expect(
      transport.requests[transport.requests.length - 1].targetBranchId
    ).toBeUndefined();
    expect(snapshots).toHaveLength(1);
  });

  test('missing_blob reports, drops the event and resyncs', async () => {
    const { transport, outbox, reports, snapshots, time } = make();
    transport.ingestHandler = req =>
      Promise.resolve(
        req.events[0].type === 'notebook_snapshot'
          ? accept(req)
          : refuse(req, 'missing_blob')
      );
    outbox.enqueue(content());
    await settle();
    await tick(time, 0);
    await settle(50);
    expect(
      reports.some(r => r.kind === 'refused' && r.reason === 'missing_blob')
    ).toBe(true);
    expect(snapshots).toHaveLength(1);
  });

  test('a rejected request (4xx) is dropped and answered with a resync; 401 pauses; 5xx retries', async () => {
    const a = make();
    a.transport.ingestHandler = (req, call) =>
      call === 0
        ? Promise.reject(new TransportError('bad', 'http', 400))
        : Promise.resolve(accept(req));
    a.outbox.enqueue(content());
    await settle();
    await tick(a.time, 0);
    await settle(50);
    expect(a.snapshots).toHaveLength(1);
    const b = make();
    b.transport.ingestHandler = () =>
      Promise.reject(new TransportError('no', 'http', 401));
    b.outbox.enqueue(content());
    await settle();
    expect(b.outbox.isPaused).toBe(true);
    const c = make();
    c.transport.ingestHandler = (req, call) =>
      call === 0
        ? Promise.reject(new TransportError('boom', 'http', 503))
        : Promise.resolve(accept(req));
    c.outbox.enqueue(content());
    await settle();
    await tick(c.time, 500);
    expect(c.outbox.length).toBe(0);
  });
});

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

  test('the snapshot replaces queued content events ahead of it but keeps other events', async () => {
    const { outbox, time, transport } = make();
    let hold = true;
    transport.ingestHandler = req =>
      hold ? new Promise(() => undefined) : Promise.resolve(accept(req));
    outbox.enqueue(content()); // in flight forever
    await settle();
    const gap = ev('recording_gap_started');
    outbox.enqueue(content());
    outbox.enqueue(gap);
    outbox.enqueue(content());
    outbox.requestResync('x');
    await tick(time, 0);
    await settle(30);
    // queue: in-flight one, the gap event, the snapshot
    expect(outbox.length).toBe(3);
    expect((outbox as any).queue.map((e: any) => e.type)).toEqual([
      'cell_source_changed',
      'recording_gap_started',
      'notebook_snapshot'
    ]);
    hold = false;
  });

  test('while a run is open for minutes: the same snapshot every 2 s, one build, content behind it held then sent', async () => {
    const { outbox, snapshots, time, transport, reports } = make();
    let runOpen = true;
    transport.ingestHandler = req =>
      Promise.resolve(
        runOpen && req.events.some(e => e.type === 'notebook_snapshot')
          ? refuse(req, 'run_in_progress')
          : accept(req)
      );
    outbox.requestResync('start');
    await tick(time, 0);
    await settle(30);
    const held = [content(), content(), content()];
    held.forEach(e => outbox.enqueue(e));
    for (let s = 0; s < 180; s += 2) {
      await tick(time, 2000);
    }
    expect(snapshots).toHaveLength(1);
    const sent = transport.requests.map(r => ids(r));
    const snapId = (snapshots[0] as any).eventId;
    expect(transport.requests.length).toBeGreaterThan(80);
    expect(sent.every(x => x.length === 1 && x[0] === snapId)).toBe(true); // nothing behind it was sent
    expect(reports.filter(r => r.kind === 'resync')).toHaveLength(1);
    runOpen = false;
    await tick(time, 2000);
    await settle(50);
    expect(outbox.length).toBe(0);
    expect(snapshots).toHaveLength(1);
    const after = transport.requests
      .slice(-2)
      .reduce((a: string[], r) => a.concat(ids(r)), []);
    expect(after).toEqual(
      expect.arrayContaining(held.map(e => (e as any).eventId))
    );
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

  test('flushKeepalive hands the queue to the keepalive call and does nothing without one', async () => {
    const { outbox, transport } = make();
    transport.ingestHandler = () => new Promise(() => undefined);
    outbox.enqueue(content());
    await settle();
    outbox.enqueue(content());
    outbox.flushKeepalive();
    expect(transport.keepalive).toHaveLength(1);
    expect(transport.keepalive[0].events).toHaveLength(2);
    const bare = make();
    delete (bare.transport as any).ingestKeepalive;
    bare.outbox.enqueue(content());
    bare.outbox.flushKeepalive();
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
