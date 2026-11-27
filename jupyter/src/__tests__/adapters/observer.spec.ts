import type { RecorderEvent } from 'blaym-notebook-types';
import { NotebookObserver } from '../../adapters/jupyterlab/observer';
import type {
  ObserverDeps,
  ObserverState
} from '../../adapters/jupyterlab/observer';
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

function setup(
  cells: FakeCell[],
  opts: { nbformat?: [number, number]; election?: 'active' | 'inactive' } = {}
) {
  const shared = new FakeShared(cells, ...(opts.nbformat ?? [4, 5]));
  const panel = new FakePanel(shared);
  const time = new FakeTime();
  const transport = new CapturingTransport();
  const election = new FakeElection(opts.election);
  const states: ObserverState[] = [];
  const deps: ObserverDeps = {
    transport,
    clock: time,
    scheduler: time,
    ids: new UuidIds(),
    election,
    policy: POLICY,
    sha256Hex: async t => `hash:${t.length}`,
    onState: s => states.push(s)
  };
  const observer = new NotebookObserver(panel, deps);
  const sent = (): RecorderEvent[] =>
    transport.requests.reduce(
      (all: RecorderEvent[], r) => all.concat(r.events as RecorderEvent[]),
      []
    );
  const types = (): string[] => sent().map(e => e.type);
  return {
    shared,
    panel,
    time,
    transport,
    election,
    observer,
    states,
    sent,
    types
  };
}

const code = (id: string, src: string) => new FakeCell(id, 'code', src);

async function started(...args: Parameters<typeof setup>) {
  const t = setup(...args);
  await t.observer.start();
  await settle();
  t.time.advance(10);
  await settle();
  return t;
}

describe('observer wiring', () => {
  it('registers after ready, then records the initial snapshot', async () => {
    const t = await started([code('c1', 'x = 1')]);
    expect(t.transport.registrations[0]).toMatchObject({
      path: 'a.ipynb',
      editor: 'jupyterlab',
      cellIdsStable: true,
      cellAliases: {},
      fingerprint: { nbformat: '4.5', cellIds: ['c1'] }
    });
    expect(t.transport.registrations[0].fingerprint.sourceHash).toMatch(
      /^hash:/
    );
    expect(t.transport.registrations[0].instanceId).toBe(t.observer.instanceId);
    expect(t.election.acquired).toEqual(['nb1']);
    expect(t.types()).toEqual(['notebook_snapshot']);
    expect(t.states[t.states.length - 1]).toEqual({ kind: 'recording' });
  });

  it('records typing, insert and delete after the snapshot', async () => {
    const t = await started([code('c1', 'x = 1')]);
    t.shared.cells[0].type('x = 2');
    t.shared.insert(1, code('c2', 'y'));
    t.time.advance(1000);
    await settle();
    t.shared.cells.splice(1, 1);
    t.shared.changed.emit();
    t.time.advance(10);
    await settle();
    expect(t.types()).toEqual([
      'notebook_snapshot',
      'cell_inserted',
      'cell_source_changed',
      'cell_deleted'
    ]);
  });
});
