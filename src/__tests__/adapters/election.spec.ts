import { WebLocksElection } from '../../adapters/jupyterlab/election';
import type { LockManagerLike } from '../../adapters/jupyterlab/election';
import { settle } from './fakes';

/** A Web Locks stand-in: `ifAvailable` gets null while the name is held. */
class FakeLocks implements LockManagerLike {
  held = new Set<string>();
  names: string[] = [];
  async request(
    name: string,
    _o: { ifAvailable: boolean },
    cb: (lock: unknown) => Promise<unknown> | unknown
  ): Promise<unknown> {
    this.names.push(name);
    if (this.held.has(name)) {
      return cb(null);
    }
    this.held.add(name);
    try {
      return await cb({ name });
    } finally {
      this.held.delete(name);
    }
  }
}

describe('WebLocksElection', () => {
  it('the first tab wins, the second is inactive', async () => {
    const locks = new FakeLocks();
    const a = new WebLocksElection(locks);
    const b = new WebLocksElection(locks);
    expect(await a.acquire('nb1')).toBe('active');
    expect(await b.acquire('nb1')).toBe('inactive');
    expect(locks.names).toEqual(['blaym-jupyter:nb1', 'blaym-jupyter:nb1']);
  });

  it('another notebook has its own lock', async () => {
    const locks = new FakeLocks();
    expect(await new WebLocksElection(locks).acquire('nb1')).toBe('active');
    expect(await new WebLocksElection(locks).acquire('nb2')).toBe('active');
  });

  it('releasing frees the lock and tells onLost', async () => {
    const locks = new FakeLocks();
    const a = new WebLocksElection(locks);
    const lost = jest.fn();
    a.onLost(lost);
    await a.acquire('nb1');
    a.releaseLock();
    await settle();
    expect(lost).toHaveBeenCalledTimes(1);
    expect(await new WebLocksElection(locks).acquire('nb1')).toBe('active');
  });

  it('without Web Locks the tab counts as active', async () => {
    expect(await new WebLocksElection(undefined as any).acquire('nb1')).toBe(
      'active'
    );
  });
});
