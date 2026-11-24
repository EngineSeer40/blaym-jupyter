import { NotebookObserver } from '../../adapters/jupyterlab/observer';
import type { CellModelLike } from '../../adapters/jupyterlab/observer';
import {
  CapturingTransport,
  FakeCell,
  FakeElection,
  FakePanel,
  FakeShared,
  FakeSignal,
  FakeTime,
  POLICY,
  UuidIds,
  settle
} from './fakes';

class FakeOutputs {
  changed = new FakeSignal();
  constructor(public length: number) {}
}

async function setup() {
  const cell = new FakeCell('c1', 'code', 'print(1)', [
    { output_type: 'stream', name: 'stdout', text: '1\n' }
  ]);
  const other = new FakeCell('c2', 'code', 'x', [
    { output_type: 'stream', name: 'stdout', text: 'k\n' }
  ]);
  const shared = new FakeShared([cell, other]);
  const panel = new FakePanel(shared);
  const outs = [new FakeOutputs(1), new FakeOutputs(1)];
  const models: CellModelLike[] = [
    { sharedModel: cell, outputs: outs[0] },
    { sharedModel: other, outputs: outs[1] }
  ];
  Object.assign(panel.model, { cells: models });
  const time = new FakeTime();
  const transport = new CapturingTransport();
  const observer = new NotebookObserver(panel, {
    transport,
    clock: time,
    scheduler: time,
    ids: new UuidIds(),
    election: new FakeElection('active'),
    policy: POLICY,
    sha256Hex: async t => `hash:${t.length}`
  });
  await observer.start();
  await settle();
  time.advance(10);
  await settle();
  const snapshots = (): number =>
    transport.requests.reduce(
      (n, r) => n + r.events.filter(e => e.type === 'notebook_snapshot').length,
      0
    );
  const kernel = panel.session.kernel!;
  return {
    kernel,
    cell,
    other,
    outs,
    observer,
    time,
    snapshots,
    models,
    shared
  };
}

describe('cleared outputs', () => {
  it('an emptied output list outside a run asks for a resync', async () => {
    const t = await setup();
    expect(t.snapshots()).toBe(1);
    t.cell.outputs = [];
    t.outs[0].length = 0;
    t.outs[0].changed.emit();
    t.time.advance(6000);
    await settle();
    expect(t.snapshots()).toBe(2);
  });

  it('appended output does not reach the reconciler', async () => {
    const t = await setup();
    t.outs[0].length = 2;
    t.outs[0].changed.emit();
    t.time.advance(10);
    await settle();
    expect(t.snapshots()).toBe(1);
  });

  it('an emptied list during a run is not a resync', async () => {
    const t = await setup();
    t.kernel.send('execute_request', 'c1', 'm1');
    t.cell.outputs = [];
    t.outs[0].length = 0;
    t.outs[0].changed.emit();
    t.time.advance(10);
    await settle();
    expect(t.snapshots()).toBe(1);
  });

  it('removed cells are unsubscribed, added ones subscribed', async () => {
    const t = await setup();
    const extra = new FakeCell('c3', 'code', 'z', []);
    const extraOut = new FakeOutputs(0);
    t.models.push({ sharedModel: extra, outputs: extraOut });
    t.shared.insert(2, extra);
    expect(extraOut.changed.slots.size).toBe(1);
    t.models.splice(1, 1);
    t.shared.cells.splice(1, 1);
    t.shared.changed.emit();
    expect(t.outs[1].changed.slots.size).toBe(0);
    t.observer.dispose();
    expect(t.outs[0].changed.slots.size).toBe(0);
    expect(extraOut.changed.slots.size).toBe(0);
  });
});
