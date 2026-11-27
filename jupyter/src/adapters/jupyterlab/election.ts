import type { Disposer, TabElection } from 'blaym-notebook-core';

/** The part of `navigator.locks` the election uses. */
export interface LockManagerLike {
  request(
    name: string,
    options: { ifAvailable?: boolean; signal?: AbortSignal },
    callback: (lock: unknown) => Promise<unknown> | unknown
  ): Promise<unknown>;
}

/**
 * Which tab of this browser records a notebook: the first to take the
 * Web Lock named for the notebook keeps it until the page closes (or
 * `release`). Others get `inactive` at once (`ifAvailable`). The server's own
 * registry decides across browsers and editors. Without Web Locks the tab
 * counts as active and the server's rule applies alone.
 */
export class WebLocksElection implements TabElection {
  private release?: () => void;
  private abort?: AbortController;
  private readonly lostCallbacks = new Set<() => void>();

  constructor(
    private readonly locks: LockManagerLike | undefined = typeof navigator !==
    'undefined'
      ? (navigator as unknown as { locks?: LockManagerLike }).locks
      : undefined
  ) {}

  acquire(notebookId: string): Promise<'active' | 'inactive'> {
    const locks = this.locks;
    if (!locks) {
      return Promise.resolve('active');
    }
    return new Promise(resolve => {
      void locks
        .request(`blaym-jupyter:${notebookId}`, { ifAvailable: true }, lock => {
          if (!lock) {
            resolve('inactive');
            return undefined;
          }
          resolve('active');
          return new Promise<void>(done => {
            this.release = () => {
              this.release = undefined;
              done();
            };
          });
        })
        .catch(() => resolve('active'));
    });
  }

  /**
   * Queue for the lock without `ifAvailable`: resolves true once the holder
   * has gone away and this tab holds it, false when cancelled.
   */
  takeover(notebookId: string): Promise<boolean> {
    const locks = this.locks;
    if (!locks) {
      return Promise.resolve(false);
    }
    const abort = new AbortController();
    this.abort = abort;
    return new Promise(resolve => {
      locks
        .request(
          `blaym-jupyter:${notebookId}`,
          { signal: abort.signal },
          () => {
            resolve(true);
            return new Promise<void>(done => {
              this.release = () => {
                this.release = undefined;
                done();
              };
            });
          }
        )
        .catch(() => resolve(false));
    });
  }

  cancelTakeover(): void {
    this.abort?.abort();
    this.abort = undefined;
  }

  onLost(cb: () => void): Disposer {
    this.lostCallbacks.add(cb);
    return () => this.lostCallbacks.delete(cb);
  }

  /** Give the lock up (the recorder stops); tells `onLost` listeners. */
  releaseLock(): void {
    if (this.release) {
      this.release();
      this.lostCallbacks.forEach(cb => cb());
    }
  }
}
