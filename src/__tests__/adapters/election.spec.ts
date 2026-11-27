import { WebLocksElection } from '../../adapters/jupyterlab/election';
import type { LockManagerLike } from '../../adapters/jupyterlab/election';

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
});
