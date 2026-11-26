import { NotebookObserver } from '../../adapters/jupyterlab/observer';
import { TransportError } from '../../recording/transport';
import {
  CapturingTransport,
  FakeCell,
  FakeElection,
  FakePanel,
  FakeShared,
  FakeTime,
  POLICY,
  UuidIds,
  settle,
  status
} from './fakes';

declare const process: any;

async function setup(
  opts: {
    takeover?: () => Promise<boolean>;
    election?: 'active' | 'inactive';
  } = {},
  start = true
) {
  const cell = new FakeCell('c1', 'code', 'x = 1', [
    { output_type: 'stream', name: 'stdout', text: '1\n' }
  ]);
  const shared = new FakeShared([cell]);
  const panel = new FakePanel(shared);
  const time = new FakeTime();
  const transport = new CapturingTransport();
  const election = Object.assign(new FakeElection(opts.election ?? 'active'), {
    takeover: opts.takeover,
    cancelTakeover: () => undefined
  });
  const observer = new NotebookObserver(panel, {
    transport,
    clock: time,
    scheduler: time,
    ids: new UuidIds(),
    election,
    policy: POLICY,
    sha256Hex: async t => `hash:${t.length}`
  });
  if (start) {
    await observer.start();
    await settle();
    time.advance(10);
    await settle();
  }
  const events = () =>
    transport.requests.reduce((a: any[], r) => a.concat(r.events as any[]), []);
  const snapshots = (): number =>
    events().filter(e => e.type === 'notebook_snapshot').length;
  return {
    cell,
    shared,
    panel,
    time,
    transport,
    observer,
    events,
    snapshots
  };
}

describe('observer lifecycle', () => {
  it('dispose unsubscribes model, cells, kernel and context signals', async () => {
    const t = await setup();
    const kernel = t.panel.kernel!;
    expect(kernel.anyMessage.slots.size).toBe(1);
    t.observer.dispose();
    expect(t.shared.changed.slots.size).toBe(0);
    expect(t.cell.changed.slots.size).toBe(0);
    expect(t.panel.saveState.slots.size).toBe(0);
    expect(t.panel.kernelChanged.slots.size).toBe(0);
    expect(t.panel.connectionStatusChanged.slots.size).toBe(0);
    // production bug candidate: kernel.anyMessage handler left connected
    expect(kernel.anyMessage.slots.size).toBe(0);
  });

  it('nothing is sent after dispose', async () => {
    const t = await setup();
    t.observer.dispose();
    const n = t.transport.requests.length;
    t.cell.type('x = 2');
    t.time.advance(60000);
    await settle();
    expect(t.transport.requests.length).toBe(n);
  });

  it('a snapshot accepted while recording is going off does not close the switched-off gap', async () => {
    const t = await setup();
    t.observer.modelReplaced(); // a resync is wanted
    let release: () => void = () => undefined;
    t.transport.registerHandler = () =>
      new Promise(r => {
        release = () => r(status({ recording: false }));
      });
    const going = t.observer.setRecording(false);
    await settle();
    t.time.advance(6000); // the snapshot is built and accepted before the server says "off"
    await settle();
    expect(t.snapshots()).toBe(2);
    release();
    await going;
    await settle();
    const types = t.events().map(e => e.type);
    expect(types).toContain('recording_gap_started');
    expect(types).not.toContain('recording_gap_ended');
  });

  it('execute_request without cellId falls back to flushAll', async () => {
    const t = await setup();
    t.cell.type('x = 42');
    t.panel.kernel!.send('execute_request', undefined, 'm9');
    await settle();
    expect(t.events().map(e => e.type)).toContain('cell_source_changed');
  });

  it('beforeRun translates a model id through the identity table (unstable ids)', async () => {
    const t = await setup();
    // nbformat 4.5 here: identity is the model id itself
    t.cell.type('x = 5');
    t.panel.kernel!.send('execute_request', 'c1', 'm2');
    await settle();
    const ev = t.events().find(e => e.type === 'cell_source_changed');
    expect(ev.cellId ?? ev.payload.cellId).toBe('c1');
  });

  it('recording off: queued typing and events are not sent, and on again resyncs once', async () => {
    const t = await setup();
    t.cell.type('x = 2'); // buffered typing
    t.transport.registerHandler = () =>
      Promise.resolve(status({ recording: false }));
    t.transport.statusHandler = t.transport.registerHandler;
    await t.observer.setRecording(false);
    const before = t.transport.requests.length;
    t.time.advance(60000);
    await settle();
    t.cell.type('x = 3');
    t.time.advance(60000);
    await settle();
    expect(t.transport.requests.length).toBe(before);
    expect(t.events().map(e => e.type)).not.toContain('cell_source_changed');
    // Task 20: the off stretch is one gap, written as it goes off
    const off = t.events().find(e => e.type === 'recording_gap_started');
    expect(off.payload).toMatchObject({
      source: 'browser',
      reason: 'switched_off'
    });
    t.transport.registerHandler = () =>
      Promise.resolve(status({ recording: true }));
    t.transport.statusHandler = t.transport.registerHandler;
    await t.observer.setRecording(true);
    await settle();
    t.time.advance(10);
    await settle();
    expect(t.snapshots()).toBe(2);
    // closed once, after the first snapshot that follows switching on
    const types = t.events().map(e => e.type);
    expect(types.filter(x => x === 'recording_gap_ended')).toHaveLength(1);
    expect(types.lastIndexOf('notebook_snapshot')).toBeLessThan(
      types.indexOf('recording_gap_ended')
    );
  });

  it('a Transport that rejects everywhere never throws into the notebook', async () => {
    const t = await setup();
    const boom = () => Promise.reject(new Error('net'));
    t.transport.ingestHandler = boom as any;
    t.transport.statusHandler = boom;
    t.transport.registerHandler = boom;
    const unhandled = jest.fn();
    process.on('unhandledRejection', unhandled);
    expect(() => {
      t.cell.type('x = 9');
      t.shared.insert(1, new FakeCell('c2', 'code', 'y'));
      t.panel.kernel!.send('execute_request', 'c1', 'mm');
      t.observer.hide('pagehide');
    }).not.toThrow();
    t.time.advance(60000);
    await settle();
    await t.observer.setRecording(true);
    await settle();
    process.off('unhandledRejection', unhandled);
    expect(unhandled).not.toHaveBeenCalled();
  });

  it('a panel disposed during register builds nothing and takes no heartbeat', async () => {
    const t = await setup({}, false);
    const p = t.observer.start();
    t.observer.dispose();
    await p;
    await settle();
    t.time.advance(60000);
    await settle();
    expect(t.transport.statuses).toBe(0);
    expect(t.events()).toEqual([]);
  });
});

describe('takeover', () => {
  it('a tab that lost the election takes over: one snapshot, one heartbeat, one subscription', async () => {
    let win!: (v: boolean) => void;
    const t = await setup(
      {
        election: 'inactive',
        takeover: () => new Promise<boolean>(r => (win = r))
      },
      false
    );
    await t.observer.start();
    await settle();
    expect(t.events()).toEqual([]);
    expect(t.panel.kernel!.anyMessage.slots.size).toBe(1);
    expect(t.shared.changed.slots.size).toBe(1);
    win(true);
    await settle();
    t.time.advance(10);
    await settle();
    expect(t.snapshots()).toBe(1);
    const statuses = t.transport.statuses;
    t.time.advance(15000);
    await settle();
    expect(t.transport.statuses - statuses).toBe(1); // a single heartbeat timer
    expect(t.panel.kernel!.anyMessage.slots.size).toBe(1);
    expect(t.shared.changed.slots.size).toBe(1);
    expect(t.cell.changed.slots.size).toBe(1);
    t.cell.type('x = 2');
    t.time.advance(6000);
    await settle();
    expect(
      t.events().filter(e => e.type === 'cell_source_changed')
    ).toHaveLength(1);
  });

  it('a takeover that resolves false or after dispose does nothing', async () => {
    let win!: (v: boolean) => void;
    const t = await setup(
      {
        election: 'inactive',
        takeover: () => new Promise<boolean>(r => (win = r))
      },
      false
    );
    await t.observer.start();
    t.observer.dispose();
    win(true);
    await settle();
    t.time.advance(60000);
    await settle();
    expect(t.events()).toEqual([]);
    expect(t.transport.statuses).toBe(0);
  });
});

import { NotebookPresenter } from '../../adapters/jupyterlab/status';

describe('presenter is quiet (S5-R)', () => {
  it('repeating the same state (every heartbeat) notifies once; recording and off never notify', () => {
    const notify = jest.fn();
    const p = new NotebookPresenter({ notify }, () => undefined);
    for (let i = 0; i < 5; i++) {
      p.onState({ kind: 'recording' });
      p.onState({ kind: 'inactive', reason: 'other_recorder' });
    }
    // alternation is a real state change each time, so only same-state repeats are tested below
    notify.mockClear();
    const q = new NotebookPresenter({ notify }, () => undefined);
    q.onState({ kind: 'starting' });
    q.onState({ kind: 'recording' });
    q.onState({ kind: 'off' });
    expect(notify).not.toHaveBeenCalled();
    for (let i = 0; i < 5; i++) {
      q.onState({ kind: 'inactive', reason: 'other_tab' });
    }
    expect(notify).toHaveBeenCalledTimes(1);
    for (let i = 0; i < 5; i++) {
      q.onState({ kind: 'problem', detail: 'boom' });
    }
    expect(notify).toHaveBeenCalledTimes(2);
  });
});

describe('the status stays honest (LT-4)', () => {
  const refuse =
    (reason: string) =>
    (req: any): Promise<any> =>
      Promise.resolve({
        apiVersion: 1,
        outcomes: req.events.map((e: any) => ({
          status: 'refused',
          eventId: e.eventId,
          reason,
          detail: 'no'
        }))
      });

  async function run(
    handler: (req: any) => Promise<any>
  ): Promise<{ states: string[]; recover: () => Promise<string> }> {
    const t = await setup();
    const kind = (): string => (t.observer as any).state.kind;
    const accepting = t.transport.ingestHandler;
    const states: string[] = [];
    const step = async (source: string): Promise<void> => {
      t.cell.type(source);
      t.time.advance(6000);
      await settle();
    };
    t.transport.ingestHandler = handler;
    await step('x = 2');
    states.push(kind());
    return {
      states,
      recover: async () => {
        t.transport.ingestHandler = accepting;
        (t.observer as any).outbox.resume();
        await step('x = 3');
        return kind();
      }
    };
  }

  it.each(['run_in_progress', 'invalid_event', 'unknown_branch'])(
    'a %s refusal is not a problem',
    async reason => {
      const r = await run(refuse(reason));
      expect(r.states).toEqual(['recording']);
    }
  );

  it('a rejected batch is not a problem', async () => {
    const r = await run(() =>
      Promise.reject(new TransportError('bad request', 'http', 400))
    );
    expect(r.states).toEqual(['recording']);
  });

  it('a paused outbox is a problem until an event is accepted again', async () => {
    const r = await run(refuse('unauthorized'));
    expect(r.states).toEqual(['problem']);
    expect(await r.recover()).toBe('recording');
  });
});
