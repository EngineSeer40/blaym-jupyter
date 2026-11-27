import type { NotebookRegistration } from 'blaym-notebook-types';
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
    registration: () =>
      ({
        path: 'a.ipynb',
        instanceId: 'tab1',
        editor: 'jupyterlab',
        cellIdsStable: true,
        cellAliases: {},
        fingerprint: { nbformat: '4.5', sourceHash: 'h' }
      }) as NotebookRegistration,
    onActive: a => log.push(`active:${a}`),
    onResync: r => log.push(`resync:${r}`),
    snapshotPending: () => pending,
    closeGap: g => gaps.push(g.gapId),
    ...over
  });
  return {
    time,
    transport,
    log,
    gaps,
    hb,
    setPending: (v: boolean) => (pending = v)
  };
}
/** Advance in 1 s steps so that each beat's answer settles before the next timer is set. */
const go = async (time: FakeTime, ms: number) => {
  for (let left = ms; left > 0; left -= 1000) {
    time.advance(Math.min(1000, left));
    await settle();
  }
};

describe('Heartbeat', () => {
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
    expect(log).toEqual([
      'active:false',
      'active:true',
      'resync:became_active'
    ]);
  });
});
