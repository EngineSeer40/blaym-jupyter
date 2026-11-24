import { WebLocksElection } from '../../adapters/jupyterlab/election';
import type { LockManagerLike } from '../../adapters/jupyterlab/election';
import { NotebookObserver } from '../../adapters/jupyterlab/observer';
import type { ObserverState } from '../../adapters/jupyterlab/observer';
import { LiveSettings } from '../../adapters/jupyterlab/settings';
import {
  NotebookPresenter,
  describeState,
  toggleTarget
} from '../../adapters/jupyterlab/status';
import type { Severity } from '../../adapters/jupyterlab/status';
import { createSignals } from '../../adapters/jupyterlab/signals';
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
  settle,
  status
} from './fakes';

function presenter() {
  const lines: Array<[Severity, string]> = [];
  const shown: string[] = [];
  const p = new NotebookPresenter({ notify: (s, t) => lines.push([s, t]) }, d =>
    shown.push(d.key)
  );
  return { p, lines, shown };
}

describe('status text and notifications', () => {
  it('describes every state in plain words', () => {
    const states: ObserverState[] = [
      { kind: 'starting' },
      { kind: 'recording' },
      { kind: 'off' },
      { kind: 'inactive', reason: 'other_tab' },
      { kind: 'inactive', reason: 'other_recorder' },
      { kind: 'inactive', reason: 'collaboration' },
      { kind: 'problem', detail: 'unauthorized' },
      { kind: 'problem', detail: 'http 500' }
    ];
    expect(states.map(s => describeState(s).key)).toEqual([
      'starting',
      'recording',
      'off',
      'other-tab',
      'other-recorder',
      'collaboration',
      'paused',
      'problem'
    ]);
    expect(describeState({ kind: 'problem', detail: 'http 500' }).label).toBe(
      'Blaym: problem (http 500)'
    );
  });

  it('normal states say nothing', () => {
    const { p, lines } = presenter();
    p.onState({ kind: 'starting' });
    p.onState({ kind: 'recording' });
    p.onState({ kind: 'off' });
    p.onState({ kind: 'recording' });
    expect(lines).toEqual([]);
  });

  it('another tab, another editor and collaboration each say one line, once', () => {
    const { p, lines } = presenter();
    p.onState({ kind: 'inactive', reason: 'other_tab' });
    p.onState({ kind: 'inactive', reason: 'other_tab' });
    p.onState({ kind: 'inactive', reason: 'other_recorder' });
    p.onState({ kind: 'inactive', reason: 'collaboration' });
    expect(lines.map(l => l[1])).toEqual([
      'Another tab is recording this notebook. This tab records nothing.',
      'Another editor is recording this notebook.',
      'Collaboration mode is not supported yet. Recording stays off.'
    ]);
  });

  it('a real problem is an error line with the reason', () => {
    const { p, lines } = presenter();
    p.onState({ kind: 'problem', detail: 'snapshot_refused' });
    expect(lines).toEqual([
      [
        'error',
        'Blaym stopped recording: the server refused the notebook state.'
      ]
    ]);
  });

  it('moved and copied notices are shown once each', () => {
    const { p, lines } = presenter();
    p.onStatus(status({ notices: [{ code: 'moved' }] }));
    p.onStatus(status({ notices: [{ code: 'moved' }, { code: 'copied' }] }));
    expect(lines).toHaveLength(2);
    expect(lines[0][1]).toMatch(/moved/);
    expect(lines[1][1]).toMatch(/copy/);
  });

  it('a kernel restart is one short line, not one per status change', () => {
    const { p, lines } = presenter();
    [
      'idle',
      'busy',
      'restarting',
      'restarting',
      'idle',
      'autorestarting'
    ].forEach(s => p.onKernelStatus(s));
    expect(lines.map(l => l[1])).toEqual([
      'Kernel restarted.',
      'Kernel restarted.'
    ]);
  });

  it('the toggle switches off and on, and does nothing elsewhere', () => {
    expect(toggleTarget({ kind: 'off' })).toBe(true);
    expect(toggleTarget({ kind: 'recording' })).toBe(false);
    expect(toggleTarget({ kind: 'starting' })).toBeUndefined();
    expect(
      toggleTarget({ kind: 'inactive', reason: 'collaboration' })
    ).toBeUndefined();
  });
});

describe('settings', () => {
  it('defaults, then live values from the registry', () => {
    const changed = new FakeSignal();
    const source = {
      composite: { typingPauseMs: 400, heartbeatMs: 'x' } as Record<
        string,
        unknown
      >,
      changed
    };
    const s = new LiveSettings(source);
    expect(s.observer.pauseMs).toBe(400);
    expect(s.observer.maxMs).toBe(5000);
    expect(s.observer.heartbeatMs).toBe(15000);
    source.composite = { typingMaxMs: 9000 };
    changed.emit();
    expect(s.observer.maxMs).toBe(9000);
    expect(s.observer.pauseMs).toBe(800);
    expect(new LiveSettings().current.aiWindowMs).toBe(250);
  });

  it('the signals stub offers none', () => {
    expect(createSignals()).toBeUndefined();
  });
});

function setup(
  opts: { collaborative?: boolean; election?: FakeElection } = {}
) {
  const shared = new FakeShared([new FakeCell('c1', 'code', 'x')]);
  const panel = new FakePanel(shared);
  panel.model = Object.assign(panel.model, {
    collaborative: opts.collaborative
  });
  const time = new FakeTime();
  const transport = new CapturingTransport();
  const states: ObserverState[] = [];
  const statuses: unknown[] = [];
  const observer = new NotebookObserver(panel, {
    transport,
    clock: time,
    scheduler: time,
    ids: new UuidIds(),
    election: opts.election ?? new FakeElection('active'),
    policy: POLICY,
    sha256Hex: async t => `hash:${t.length}`,
    onState: s => states.push(s),
    onStatus: s => statuses.push(s)
  });
  return { panel, transport, observer, states, statuses, time };
}

describe('collaboration and status through the observer', () => {
  it('a collaborative notebook registers collaboration: true and stays off', async () => {
    const t = setup({ collaborative: true });
    t.transport.registerHandler = () =>
      Promise.resolve(
        status({
          recording: false,
          active: false,
          reason: 'collaboration_not_supported'
        })
      );
    await t.observer.start();
    await settle();
    expect(t.transport.registrations[0].collaboration).toBe(true);
    expect(t.states).toEqual([{ kind: 'inactive', reason: 'collaboration' }]);
    expect(t.transport.requests).toEqual([]);
  });

  it('a normal notebook does not send the collaboration flag', async () => {
    const t = setup({ collaborative: false });
    await t.observer.start();
    await settle();
    expect('collaboration' in t.transport.registrations[0]).toBe(false);
  });

  it('statuses reach onStatus (the notices)', async () => {
    const t = setup();
    t.transport.registerHandler = () =>
      Promise.resolve(status({ notices: [{ code: 'copied' }] }));
    await t.observer.start();
    await settle();
    expect(t.statuses.length).toBeGreaterThan(0);
  });

  it('setRecording(false) registers the flag and shows off', async () => {
    const t = setup();
    await t.observer.start();
    await settle();
    t.transport.registerHandler = () =>
      Promise.resolve(status({ recording: false }));
    await t.observer.setRecording(false);
    expect(t.transport.registrations.pop()).toMatchObject({
      setRecording: false
    });
    expect(t.states[t.states.length - 1]).toEqual({ kind: 'off' });
  });
});

class QueueLocks implements LockManagerLike {
  holder?: { release: () => void };
  waiting: Array<() => void> = [];
  async request(
    _name: string,
    options: { ifAvailable?: boolean; signal?: AbortSignal },
    cb: (lock: unknown) => Promise<unknown> | unknown
  ): Promise<unknown> {
    const run = async (): Promise<unknown> => {
      let release!: () => void;
      const held = new Promise<void>(r => (release = r));
      this.holder = { release };
      const result = cb({}) as Promise<unknown>;
      void result.then(() => undefined);
      await Promise.race([result, held]);
      return result;
    };
    if (!this.holder) {
      const p = run();
      void p.finally(() => this.next());
      return p;
    }
    if (options.ifAvailable) {
      return cb(null);
    }
    return new Promise(resolve => {
      this.waiting.push(() => {
        const p = run();
        void p.finally(() => this.next());
        resolve(p);
      });
    });
  }
  private next(): void {
    this.holder = undefined;
    this.waiting.shift()?.();
  }
}

describe('election retry', () => {
  it('a tab that lost takes over when the holder goes away, then resyncs', async () => {
    const locks = new QueueLocks();
    const first = new WebLocksElection(locks);
    expect(await first.acquire('nb1')).toBe('active');
    const t = setup({ election: new WebLocksElection(locks) as never });
    await t.observer.start();
    await settle();
    expect(t.states[t.states.length - 1]).toEqual({
      kind: 'inactive',
      reason: 'other_tab'
    });
    expect(t.transport.requests).toEqual([]);
    first.releaseLock();
    await settle();
    await settle();
    t.time.advance(10);
    await settle();
    expect(t.states[t.states.length - 1]).toEqual({ kind: 'recording' });
    expect(
      t.transport.requests.some(r =>
        r.events.some(e => e.type === 'notebook_snapshot')
      )
    ).toBe(true);
    expect(t.transport.registers).toBeGreaterThanOrEqual(2);
  });

  it('disposing while waiting cancels the takeover', async () => {
    const cancelled = jest.fn();
    const election = Object.assign(new FakeElection('inactive'), {
      takeover: () => new Promise<boolean>(() => undefined),
      cancelTakeover: cancelled
    });
    const t = setup({ election });
    await t.observer.start();
    t.observer.dispose();
    expect(cancelled).toHaveBeenCalled();
  });
});

describe('settings intervals and a disposed panel', () => {
  it('zero or negative intervals fall back to the defaults', () => {
    const s = new LiveSettings({
      composite: {
        heartbeatMs: 0,
        typingPauseMs: 0,
        typingMaxMs: -5,
        retryFirstMs: 0,
        aiWindowMs: 0
      },
      changed: new FakeSignal()
    });
    expect(s.observer.heartbeatMs).toBe(15000);
    expect(s.observer.pauseMs).toBe(800);
    expect(s.observer.maxMs).toBe(5000);
    expect(s.current.retryFirstMs).toBe(500);
    expect(s.current.aiWindowMs).toBe(0); // a zero AI window is allowed
  });

  it('a panel disposed during register releases the lock it just took', async () => {
    const t = setup();
    const release = jest.fn();
    const election = Object.assign(new FakeElection('active'), {
      releaseLock: release
    });
    const panel = t.panel;
    const o = new NotebookObserver(panel, {
      transport: t.transport,
      clock: t.time,
      scheduler: t.time,
      ids: new UuidIds(),
      election,
      policy: POLICY,
      sha256Hex: async x => `hash:${x.length}`
    });
    let answer!: () => void;
    t.transport.registerHandler = () =>
      new Promise(resolve => {
        answer = () => resolve(status());
      });
    const started = o.start();
    await settle();
    o.dispose(); // while the registration is in flight
    answer();
    await started;
    await settle();
    expect(release).toHaveBeenCalled();
    expect(t.transport.requests).toEqual([]);
  });
});
