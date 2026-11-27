import type {
  IngestRequest,
  IngestResponse,
  NotebookRegistration,
  NotebookRenameRequest,
  NotebookSaveNotice,
  NotebookStatus,
  PrepareOutputsRequest,
  PrepareOutputsResponse
} from 'blaym-notebook-types';
import {
  TransportError,
  type Transport
} from '../../core/ts/src/recording/transport';
import {
  ROUTES,
  routePath,
  type RouteName
} from '../../core/ts/src/recording/routes';

/** The core `Transport` over the local service's HTTP API, with the bearer token. */
export class ServiceTransport implements Transport {
  constructor(
    private readonly port: number,
    private readonly token: string
  ) {}

  ingest(req: IngestRequest): Promise<IngestResponse> {
    return this.call('ingest', req);
  }

  prepareOutputs(req: PrepareOutputsRequest): Promise<PrepareOutputsResponse> {
    return this.call('prepare_outputs', req);
  }

  register(req: NotebookRegistration): Promise<NotebookStatus> {
    return this.call('register', req);
  }

  status(notebookId: string, instanceId: string): Promise<NotebookStatus> {
    return this.call('status', undefined, { notebookId }, { instanceId });
  }

  rename(req: NotebookRenameRequest): Promise<NotebookStatus> {
    return this.call('rename', req);
  }

  saveNotice(req: NotebookSaveNotice): Promise<void> {
    return this.call('save', req);
  }

  /** Fire and forget; never throws. */
  close(notebookId: string, instanceId: string): void {
    try {
      this.send('close', { notebookId, instanceId }).catch(() => undefined);
    } catch {
      // the window is going away: nothing to do
    }
  }

  private send(
    name: RouteName,
    body?: unknown,
    params: Record<string, string> = {},
    query?: Record<string, string>
  ): Promise<Response> {
    const qs = query ? '?' + new URLSearchParams(query).toString() : '';
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.token}`
    };
    const init: RequestInit = { method: ROUTES[name].method, headers };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      headers['Content-Type'] = 'application/json';
    }
    return fetch(
      `http://127.0.0.1:${this.port}${routePath(name, params)}${qs}`,
      init
    );
  }

  private async call<T>(
    name: RouteName,
    body?: unknown,
    params?: Record<string, string>,
    query?: Record<string, string>
  ): Promise<T> {
    let response: Response;
    try {
      response = await this.send(name, body, params, query);
    } catch (e) {
      throw new TransportError(String(e), 'network');
    }
    if (!response.ok) {
      throw new TransportError(
        `${name} answered ${response.status}`,
        'http',
        response.status
      );
    }
    const text = await response.text();
    return (text.length > 0 ? JSON.parse(text) : undefined) as T;
  }
}
