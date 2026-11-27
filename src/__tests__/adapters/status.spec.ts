import { WebLocksElection } from '../../adapters/jupyterlab/election';
import type { LockManagerLike } from '../../adapters/jupyterlab/election';
import { NotebookObserver } from '../../adapters/jupyterlab/observer';
import type { ObserverState } from '../../adapters/jupyterlab/observer';
import { LiveSettings } from '../../adapters/jupyterlab/settings';
import {
  NotebookPresenter,
  toggleTarget
} from '../../adapters/jupyterlab/status';
import type { Severity } from '../../adapters/jupyterlab/status';
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

function presenter() {
  const lines: Array<[Severity, string]> = [];
  const shown: string[] = [];
  const p = new NotebookPresenter({ notify: (s, t) => lines.push([s, t]) }, d =>
    shown.push(d.key)
  );
  return { p, lines, shown };
}

describe('status text and notifications', () => {
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
    expect(new LiveSettings().current.typingMaxMs).toBe(5000);
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

describe('collaboration and status through the observer', () => {});

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
});

describe('settings intervals and a disposed panel', () => {});
