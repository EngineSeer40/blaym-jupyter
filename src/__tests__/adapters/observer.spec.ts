import type { RecorderEvent } from 'blaym-jupyter-types';
import { NotebookObserver } from '../../adapters/jupyterlab/observer';
import type {
  ObserverDeps,
  ObserverState
} from '../../adapters/jupyterlab/observer';
import {
  CapturingTransport,
  FakeCell,
  FakeElection,
  FakeKernel,
  FakePanel,
  FakeShared,
  FakeTime,
  POLICY,
  UuidIds,
  settle,
  status
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

declare const __dirname: string;
declare const require: (name: string) => any;
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { readFileSync } = require('fs');
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { join } = require('path');

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

  it('builds no event while recording is off', async () => {
    const t = setup([code('c1', 'x')]);
    t.transport.registerHandler = () =>
      Promise.resolve(status({ recording: false }));
    t.transport.statusHandler = t.transport.registerHandler;
    await t.observer.start();
    await settle();
    t.shared.cells[0].type('changed');
    t.time.advance(5000);
    await settle();
    expect(t.sent()).toEqual([]);
    expect(t.states[t.states.length - 1]).toEqual({ kind: 'off' });
  });

  it('setRecording registers with the flag and then records', async () => {
    const t = setup([code('c1', 'x')]);
    let on = false;
    t.transport.registerHandler = () =>
      Promise.resolve(status({ recording: on }));
    t.transport.statusHandler = t.transport.registerHandler;
    await t.observer.start();
    await settle();
    on = true;
    await t.observer.setRecording(true);
    await settle();
    t.time.advance(10);
    await settle();
    expect(t.transport.registrations.pop()?.setRecording).toBe(true);
    expect(t.types()).toEqual(['notebook_snapshot']);
  });

  it('an inactive tab records nothing and does not start a heartbeat', async () => {
    const t = setup([code('c1', 'x')], { election: 'inactive' });
    await t.observer.start();
    await settle();
    t.shared.cells[0].type('y');
    t.time.advance(60000);
    await settle();
    expect(t.sent()).toEqual([]);
    expect(t.transport.statuses).toBe(0);
    expect(t.states[t.states.length - 1]).toEqual({
      kind: 'inactive',
      reason: 'other_tab'
    });
  });

  it('a server that says not active records nothing', async () => {
    const t = setup([code('c1', 'x')]);
    t.transport.registerHandler = () =>
      Promise.resolve(
        status({ active: false, reason: 'other_recorder_active' })
      );
    t.transport.statusHandler = t.transport.registerHandler;
    await t.observer.start();
    await settle();
    t.time.advance(1000);
    expect(t.sent()).toEqual([]);
  });
});

describe('registration', () => {
  it('unstable ids: aligns with the tip and sends the aliases', async () => {
    const t = setup([code('rand1', 'a = 1'), code('rand2', 'b = 2')], {
      nbformat: [4, 4]
    });
    t.transport.tipCells = [
      { cellId: 'blaym-1', type: 'code', code: 'a = 1' },
      { cellId: 'blaym-2', type: 'code', code: 'b = 2' }
    ];
    t.transport.registerHandler = () =>
      Promise.resolve(
        status({ cellRevisions: { 'blaym-1': 3, 'blaym-2': 1 } })
      );
    t.transport.statusHandler = t.transport.registerHandler;
    await t.observer.start();
    await settle();
    t.time.advance(10);
    await settle();
    const first = t.transport.registrations[0];
    expect(first.cellIdsStable).toBe(false);
    expect(first.fingerprint.cellIds).toBeUndefined();
    expect(first.fingerprint.nbformat).toBe('4.4');
    const withAliases = t.transport.registrations.find(
      r => Object.keys(r.cellAliases).length > 0
    );
    expect(withAliases?.cellAliases).toEqual({
      rand1: 'blaym-1',
      rand2: 'blaym-2'
    });
    const snap = t.sent()[0] as any;
    expect(snap.type).toBe('notebook_snapshot');
    expect(snap.payload.cells.map((c: any) => [c.cellId, c.revision])).toEqual([
      ['blaym-1', 3],
      ['blaym-2', 1]
    ]);
  });
});

describe('runs', () => {
  it('beforeRun fires on an outgoing execute_request and flushes the typed code', async () => {
    const t = await started([code('c1', 'x = 1')]);
    t.shared.cells[0].type('x = 99');
    expect(t.types()).toEqual(['notebook_snapshot']);
    t.panel.kernel!.send('execute_request', 'c1');
    await settle();
    expect(t.types()).toEqual(['notebook_snapshot', 'cell_source_changed']);
    expect((t.sent()[1] as any).payload.code).toBe('x = 99');
  });

  it('ignores incoming and non-execute messages', async () => {
    const t = await started([code('c1', 'x = 1')]);
    t.shared.cells[0].type('x = 5');
    t.panel.kernel!.send('kernel_info_request');
    t.panel.kernel!.anyMessage.emit({
      direction: 'recv',
      msg: { header: { msg_type: 'execute_request' } }
    });
    await settle();
    expect(t.types()).toEqual(['notebook_snapshot']);
  });

  it('still fires after a kernel change (re-subscription)', async () => {
    const t = await started([code('c1', 'x = 1')]);
    const old = t.panel.kernel!;
    t.panel.session.kernel = new FakeKernel();
    t.panel.kernelChanged.emit();
    expect(old.anyMessage.slots.size).toBe(0);
    t.shared.cells[0].type('x = 7');
    t.panel.kernel!.send('execute_request', 'c1');
    await settle();
    expect(t.types()).toContain('cell_source_changed');
  });

  it('a connection status change on the same kernel does not double-subscribe', async () => {
    const t = await started([code('c1', 'x = 1')]);
    t.panel.connectionStatusChanged.emit();
    t.panel.connectionStatusChanged.emit();
    expect(t.panel.kernel!.anyMessage.slots.size).toBe(1);
  });
});

describe('save, hide, replace, heartbeat', () => {
  it('save flushes the typing step', async () => {
    const t = await started([code('c1', 'x')]);
    t.shared.cells[0].type('xy');
    t.panel.saveState.emit('started');
    await settle();
    expect(t.types()).toEqual(['notebook_snapshot', 'cell_source_changed']);
  });

  it('hidden flushes and sends a keepalive batch without closing', async () => {
    const t = await started([code('c1', 'x')]);
    t.transport.ingestHandler = () => new Promise(() => undefined);
    t.shared.cells[0].type('xy');
    t.observer.hide('hidden');
    await settle();
    expect(t.transport.keepalive.length).toBeGreaterThan(0);
    expect(t.transport.closed).toEqual([]);
  });

  it('pagehide flushes and closes cleanly', async () => {
    const t = await started([code('c1', 'x')]);
    t.transport.ingestHandler = () => new Promise(() => undefined);
    t.shared.cells[0].type('xy');
    t.observer.hide('pagehide');
    await settle();
    expect(t.transport.closed).toEqual([['nb1', t.observer.instanceId]]);
    expect(
      t.transport.keepalive.reduce(
        (all: string[], r) => all.concat(r.events.map(e => e.type)),
        []
      )
    ).toContain('cell_source_changed');
  });

  it('a replaced model (revert) is a resync: one new snapshot', async () => {
    const t = await started([code('c1', 'x')]);
    t.panel.model.sharedModel = new FakeShared([code('z1', 'new')]);
    t.panel.model.sharedModel.changed.emit();
    t.shared.changed.emit();
    t.time.advance(10);
    await settle();
    t.time.advance(6000);
    await settle();
    expect(t.types()).toEqual(['notebook_snapshot', 'notebook_snapshot']);
    const snap = t.sent()[1] as any;
    expect(snap.payload.cells.map((c: any) => c.cellId)).toEqual(['z1']);
  });

  it('all ids replaced inside the same model is a resync', async () => {
    const t = await started([code('c1', 'x')]);
    t.shared.cells = [code('z1', 'new')];
    t.shared.changed.emit();
    t.time.advance(6000);
    await settle();
    expect(t.types()).toEqual(['notebook_snapshot', 'notebook_snapshot']);
  });

  it('needsRegister makes the heartbeat register again with the current aliases', async () => {
    const t = await started([code('c1', 'x')]);
    const before = t.transport.registrations.length;
    t.transport.statusHandler = () =>
      Promise.resolve(status({ needsRegister: true }));
    t.time.advance(15000);
    await settle();
    expect(t.transport.registrations.length).toBe(before + 1);
  });

  it('the heartbeat runs every 15 s by default', async () => {
    const t = await started([code('c1', 'x')]);
    const n = t.transport.statuses;
    t.time.advance(15000);
    await settle();
    expect(t.transport.statuses).toBe(n + 1);
  });

  it('the switching flag stops event building when set', async () => {
    const t = setup([code('c1', 'x')]);
    let sw = false;
    (t.observer as any).deps.switching = () => sw;
    await t.observer.start();
    await settle();
    t.time.advance(10);
    await settle();
    sw = true;
    t.shared.insert(1, code('c2', 'y'));
    t.time.advance(10);
    await settle();
    expect(t.types()).toEqual(['notebook_snapshot']);
  });
});

describe('metadata policy copy', () => {
  it('is identical to the shared fixture', () => {
    const a = readFileSync(
      join(__dirname, '../../adapters/jupyterlab/metadata-policy.json'),
      'utf8'
    );
    const b = readFileSync(
      join(__dirname, '../../../fixtures/recorder/metadata-policy.json'),
      'utf8'
    );
    expect(a).toBe(b);
  });
});
