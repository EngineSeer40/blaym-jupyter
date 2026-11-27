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
});

describe('the status stays honest', () => {
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

  it.each(['invalid_event', 'unknown_branch'])(
    'a %s refusal is not a problem',
    async reason => {
      const r = await run(refuse(reason));
      expect(r.states).toEqual(['recording']);
    }
  );
});
