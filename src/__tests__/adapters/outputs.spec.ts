import { NotebookObserver } from '../../adapters/jupyterlab/observer';
import type { PanelLike } from '../../adapters/jupyterlab/observer';
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
  return { observer, cell, shared, panel, tick, events, snapshots };
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

describe('clear commands become cell_outputs_cleared', () => {
  const cleared = (t: Awaited<ReturnType<typeof setup>>): string[] =>
    t
      .events()
      .filter(e => e.type === 'cell_outputs_cleared')
      .map(e => e.payload.cellId);

  /** The cells c1 (with output), c2 (without) and c3 (with output); `selected` by id. */
  async function three(selected: string[] = []) {
    const t = await setup();
    const out = { output_type: 'stream', name: 'stdout', text: '3\n' };
    t.shared.insert(1, new FakeCell('c2', 'code', 'b'));
    t.shared.insert(2, new FakeCell('c3', 'code', 'c', [out]));
    (t.panel as PanelLike).content = {
      widgets: t.shared.cells.map(c => ({ model: { id: c.id } })),
      isSelectedOrActive: (w: any) => selected.includes(w.model.id)
    };
    await t.tick();
    // as in JupyterLab, the output is gone before the command is reported
    const run = async (command: string, emptied: number[]) => {
      emptied.forEach(i => {
        t.shared.cells[i].outputs = [];
        t.shared.cells[i].changed.emit();
      });
      t.observer.commandExecuted(command);
      await t.tick();
    };
    return { ...t, run };
  }

  it('clear all: one event for each cell that had output, and no snapshot', async () => {
    const t = await three();
    await t.run('notebook:clear-all-cell-outputs', [0, 2]);
    expect(cleared(t)).toEqual(['c1', 'c3']);
    await t.run('notebook:clear-all-cell-outputs', []);
    expect(cleared(t)).toEqual(['c1', 'c3']);
    expect(t.snapshots()).toBe(1);
  });

  it('clear cell output: only the selected cells that had output', async () => {
    const t = await three(['c2', 'c3']);
    await t.run('notebook:clear-cell-output', [2]);
    expect(cleared(t)).toEqual(['c3']);
  });

  it('emptied output without a clear command (a re-run, restart and run all) sends none', async () => {
    const t = await three();
    await t.run('notebook:restart-run-all', [0, 2]);
    await t.run('notebook:run-all-cells', []);
    expect(cleared(t)).toEqual([]);
  });
});
