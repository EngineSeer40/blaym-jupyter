import { TransportError } from '../../recording/transport';
import { ServerTransport } from '../../adapters/jupyterlab/transport';

const settings: any = { baseUrl: 'http://h/', token: '' };

function make(handler: (url: string, init: RequestInit) => Promise<any>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const t = new ServerTransport(settings, (url, init) => {
    calls.push({ url, init });
    return handler(url, init);
  });
  return { t, calls };
}

const ok = (body: unknown) => ({
  ok: true,
  status: 200,
  text: async () => JSON.stringify(body)
});

describe('ServerTransport', () => {
  it('posts register to the route table path', async () => {
    const { t, calls } = make(async () => ok({ notebookId: 'n' }));
    await expect(t.register({ path: 'a' } as any)).resolves.toEqual({
      notebookId: 'n'
    });
    expect(calls[0].url).toBe('http://h/blaym-jupyter/api/notebooks/register');
    expect(calls[0].init.method).toBe('POST');
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ path: 'a' });
  });

  it('no answer is a network error, a bad status an http error', async () => {
    const down = make(async () => Promise.reject(new Error('down')));
    await expect(down.t.ingest({} as any)).rejects.toMatchObject({
      kind: 'network'
    });
    const bad = make(async () => ({
      ok: false,
      status: 503,
      text: async () => ''
    }));
    const err = await bad.t.ingest({} as any).catch(e => e);
    expect(err).toBeInstanceOf(TransportError);
    expect(err).toMatchObject({ kind: 'http', status: 503 });
  });
});
