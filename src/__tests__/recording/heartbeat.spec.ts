import type { NotebookRegistration, NotebookStatus } from 'blaym-jupyter-types';
import { Heartbeat } from '../../recording';
import { FakeTime, FakeTransport, settle, status } from './generate';

function make(over: Partial<ConstructorParameters<typeof Heartbeat>[0]> = {}) {
  const time = new FakeTime();
  const transport = new FakeTransport();
  const log: string[] = [];
  const gaps: string[] = [];
  let pending = false;
  const hb = new Heartbeat({
    transport,
    scheduler: time,
    notebookId: 'nb1',
    instanceId: 'tab1',
    registration: () => ({ path: 'a.ipynb', instanceId: 'tab1', editor: 'jupyterlab', cellIdsStable: true, cellAliases: {}, fingerprint: { nbformat: '4.5', sourceHash: 'h' } }) as NotebookRegistration,
    onActive: a => log.push(`active:${a}`),
    onResync: r => log.push(`resync:${r}`),
    snapshotPending: () => pending,
    closeGap: g => gaps.push(g.gapId),
    ...over
  });
  return { time, transport, log, gaps, hb, setPending: (v: boolean) => (pending = v) };
}
/** Advance in 1 s steps so that each beat's answer settles before the next timer is set. */
const go = async (time: FakeTime, ms: number) => {
  for (let left = ms; left > 0; left -= 1000) {
    time.advance(Math.min(1000, left));
    await settle();
  }
};

describe('Heartbeat (S5-S)', () => {
  test('registers, then asks for status at once and every 15 s', async () => {
    const { hb, transport, time } = make();
    await hb.start();
    expect([transport.registers, transport.statuses]).toEqual([1, 1]);
    await go(time, 14_999);
    expect(transport.statuses).toBe(1);
    await go(time, 1);
    expect(transport.statuses).toBe(2);
    await go(time, 45_000);
    expect(transport.statuses).toBe(5);
  });

  test('heartbeatMs is a setting', async () => {
    const { hb, transport, time } = make({ heartbeatMs: 1000 });
    await hb.start();
    await go(time, 3000);
    expect(transport.statuses).toBe(4);
  });

  test('needsRegister registers again', async () => {
    const { hb, transport, time } = make();
    let first = true;
    transport.statusHandler = () => {
      const s = status({ needsRegister: first });
      first = false;
      return Promise.resolve(s);
    };
    await hb.start();
    expect(transport.registers).toBe(2);
    await go(time, 15_000);
    expect(transport.registers).toBe(2);
  });

  test('needsResync asks for a resync', async () => {
    const { hb, transport, log } = make();
    transport.statusHandler = () => Promise.resolve(status({ needsResync: true }));
    await hb.start();
    expect(log).toContain('resync:server');
  });

  test('an inactive answer is reported once; becoming active resyncs', async () => {
    const { hb, transport, log, time } = make();
    let active = false;
    transport.registerHandler = () => Promise.resolve(status({ active }));
    transport.statusHandler = () => Promise.resolve(status({ active }));
    await hb.start();
    await go(time, 15_000);
    expect(log).toEqual(['active:false']);
    active = true;
    await go(time, 15_000);
    expect(log).toEqual(['active:false', 'active:true', 'resync:became_active']);
  });

  test('browser-opened gaps are closed once, and only after the snapshot', async () => {
    const { hb, transport, gaps, time, setPending } = make();
    const open: NotebookStatus['openGaps'] = [
      { gapId: 'g1', source: 'browser', reason: 'no_client' },
      { gapId: 'g2', source: 'kernel', reason: 'unknown' }
    ];
    transport.statusHandler = () => Promise.resolve(status({ openGaps: open }));
    setPending(true);
    await hb.start();
    expect(gaps).toEqual([]);
    setPending(false);
    await go(time, 15_000);
    expect(gaps).toEqual(['g1']);
    await go(time, 30_000);
    expect(gaps).toEqual(['g1']);
  });

  test('errors do not stop the beat', async () => {
    const errors: string[] = [];
    const { hb, transport, time } = make({ onError: k => errors.push(k) });
    let fail = true;
    transport.statusHandler = () => (fail ? Promise.reject(new Error('x')) : Promise.resolve(status()));
    await hb.start();
    await go(time, 15_000);
    fail = false;
    await go(time, 15_000);
    expect(errors).toEqual(['status', 'status']);
    expect(transport.statuses).toBe(3);
  });

  test('close sends close once and stops the beat; stop does not send close', async () => {
    const a = make();
    await a.hb.start();
    a.hb.close();
    await go(a.time, 60_000);
    expect(a.transport.closed).toEqual([['nb1', 'tab1']]);
    expect(a.transport.statuses).toBe(1);
    const b = make();
    await b.hb.start();
    b.hb.stop();
    await go(b.time, 60_000);
    expect(b.transport.closed).toEqual([]);
    expect(b.transport.statuses).toBe(1);
  });
});
