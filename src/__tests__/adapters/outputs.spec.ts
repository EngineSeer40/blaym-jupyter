import { NotebookObserver } from '../../adapters/jupyterlab/observer';
import {
  CapturingTransport,
  FakeCell,
  FakeElection,
  FakePanel,
  FakeShared,
  FakeTime,
  POLICY,
  UuidIds,
  settle
} from './fakes';

async function setup() {
  const cell = new FakeCell('c1', 'code', 'x = 1', [
    { output_type: 'stream', name: 'stdout', text: '1\n' }
  ]);
  const shared = new FakeShared([cell]);
  const panel = new FakePanel(shared);
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
  const tick = async (): Promise<void> => {
    await settle();
    time.advance(6000);
    await settle();
  };
  await observer.start();
  await tick();
  const events = (): any[] =>
    transport.requests.reduce((a: any[], r) => a.concat(r.events), []);
  const snapshots = (): number =>
    events().filter(e => e.type === 'notebook_snapshot').length;
  return { cell, shared, panel, tick, events, snapshots };
}

describe('outputs never cause a snapshot (LT-1, LT-7)', () => {
  it('an emptied output list (a re-run, a cleared cell) sends no snapshot', async () => {
    const t = await setup();
    expect(t.snapshots()).toBe(1);
    t.panel.kernel!.send('execute_request', 'c1', 'm1');
    t.cell.outputs = [];
    t.shared.changed.emit();
    await t.tick();
    expect(t.snapshots()).toBe(1);
  });

  it('code to markdown and back is two cell_type_changed and no snapshot', async () => {
    const t = await setup();
    for (const type of ['markdown', 'code']) {
      t.shared.cells.splice(0, 1, new FakeCell('c1', type, 'x = 1'));
      t.shared.changed.emit();
      await t.tick();
    }
    expect(
      t
        .events()
        .filter(e => e.type === 'cell_type_changed')
        .map(e => e.payload.cellType)
    ).toEqual(['markdown', 'code']);
    expect(t.snapshots()).toBe(1);
  });
});
