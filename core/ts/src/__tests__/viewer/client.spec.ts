import { createClient } from '../../viewer/client';
import { createHostChannel } from '../../viewer/host';
import type { HostMessageEvent, HostPort } from '../../viewer/host';

/** A scheduler that runs timers only when the test advances it. */
function fakeScheduler() {
  const timers: { at: number; fn: () => void; live: boolean }[] = [];
  let now = 0;
  return {
    setTimeout(fn: () => void, ms: number) {
      const t = { at: now + ms, fn, live: true };
      timers.push(t);
      return () => {
        t.live = false;
      };
    },
    advance(ms: number) {
      now += ms;
      for (const t of timers) {
        if (t.live && t.at <= now) {
          t.live = false;
          t.fn();
        }
      }
    },
    pending: () => timers.filter(t => t.live).map(t => t.at - now)
  };
}

const flush = async () => {
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
};

describe('viewer client', () => {
  test('the host back end matches replies by id, ignores unknown ids and times out', async () => {
    const sent: any[] = [];
    let hear: (ev: HostMessageEvent) => void = () => undefined;
    const port: HostPort = {
      post: m => sent.push(m),
      listen: fn => {
        hear = fn;
        return () => undefined;
      }
    };
    const sched = fakeScheduler();
    const channel = createHostChannel({
      port,
      scheduler: sched,
      timeoutMs: 1000
    });
    const client = createClient({
      backend: { kind: 'host', channel },
      scheduler: sched
    });
    const reply = (data: unknown) => hear({ data, origin: 'x', source: null });

    const answered = client.outline({ explorationId: 'e1' });
    expect(sent[0]).toMatchObject({
      type: 'blaym:request',
      name: 'outline',
      params: { explorationId: 'e1' }
    });
    reply({ type: 'blaym:response', id: 'nope', ok: true, body: 'wrong' });
    reply({
      type: 'blaym:response',
      id: sent[0].id,
      ok: true,
      body: { ok: 1 }
    });
    await expect(answered).resolves.toEqual({ ok: 1 });

    const silent = client.outline({ explorationId: 'e1' });
    const failed = expect(silent).rejects.toMatchObject({ kind: 'timeout' });
    sched.advance(1000);
    await failed;
  });

  test('the stream reader parses split frames, and reconnects with backoff after a drop', async () => {
    const enc = new TextEncoder();
    const advanced = (tick: number) =>
      `event: advanced\ndata: ${JSON.stringify({
        explorationId: 'e1',
        branchId: 'b1',
        tick
      })}\n\n`;
    const urls: string[] = [];
    const closers: (() => void)[] = [];
    const feeds: ((s: string) => void)[] = [];
    const fakeFetch = (async (url: string) => {
      urls.push(url);
      let push: (v: { done: boolean; value?: Uint8Array }) => void = () =>
        undefined;
      const queue: { done: boolean; value?: Uint8Array }[] = [];
      let waiting: typeof push | undefined;
      push = v => (waiting ? waiting(v) : queue.push(v));
      feeds.push(s => push({ done: false, value: enc.encode(s) }));
      closers.push(() => push({ done: true }));
      return {
        ok: true,
        body: {
          getReader: () => ({
            read: () =>
              new Promise(resolve => {
                const q = queue.shift();
                if (q) {
                  resolve(q);
                } else {
                  waiting = v => {
                    waiting = undefined;
                    resolve(v);
                  };
                }
              })
          })
        }
      };
    }) as unknown as typeof fetch;

    const sched = fakeScheduler();
    const log: string[] = [];
    const client = createClient({
      backend: { kind: 'direct', base: '/api', fetch: fakeFetch },
      scheduler: sched
    });
    const stop = client.stream('e1', 3, {
      onAdvanced: a => log.push(`advanced ${a.tick}`),
      onDisconnected: () => log.push('disconnected'),
      onReconnected: () => log.push('reconnected')
    });
    await flush();
    expect(urls[0]).toBe('/api/stream?explorationId=e1&afterTick=3');

    const frame = advanced(4);
    feeds[0](': keep-alive\n\n' + frame.slice(0, 20));
    await flush();
    expect(log).toEqual([]);
    feeds[0](frame.slice(20) + advanced(5));
    await flush();
    expect(log).toEqual(['advanced 4', 'advanced 5']);

    closers[0]();
    await flush();
    expect(log.slice(2)).toEqual(['disconnected']);
    expect(sched.pending()).toEqual([500]);
    sched.advance(500);
    await flush();
    expect(urls[1]).toBe('/api/stream?explorationId=e1&afterTick=5');
    expect(log.slice(2)).toEqual(['disconnected', 'reconnected']);
    stop();
  });
});
