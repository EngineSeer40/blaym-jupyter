import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';
import type {
  IngestRequest,
  IngestResponse,
  NotebookRegistration,
  NotebookStatus,
  HistoryOutline,
  PrepareOutputsRequest,
  PrepareOutputsResponse,
  RebuildResult
} from 'blaym-notebook-types';
import {
  TransportError,
  ROUTE_BASE,
  ROUTES,
  routePath
} from 'blaym-notebook-core';
import type { Transport, TipCell, RouteName } from 'blaym-notebook-core';

/** `ServerConnection.makeRequest`, replaceable in tests. */
export type MakeRequest = (
  url: string,
  init: RequestInit,
  settings: ServerConnection.ISettings
) => Promise<Response>;

/**
 * The recorder's `Transport` over the Jupyter server's Blaym routes.
 * `makeRequest` adds the token or XSRF header; the keepalive calls (the last
 * batch and the clean close when the page hides) go through it too, with
 * `keepalive: true` so they may outlive the page.
 */
export class ServerTransport implements Transport {
  constructor(
    private readonly settings: ServerConnection.ISettings,
    private readonly makeRequest: MakeRequest = ServerConnection.makeRequest
  ) {}

  ingest(req: IngestRequest): Promise<IngestResponse> {
    return this.call('ingest', {}, req);
  }

  prepareOutputs(req: PrepareOutputsRequest): Promise<PrepareOutputsResponse> {
    return this.call('prepare_outputs', {}, req);
  }

  register(req: NotebookRegistration): Promise<NotebookStatus> {
    return this.call('register', {}, req);
  }

  status(notebookId: string, instanceId: string): Promise<NotebookStatus> {
    return this.call('status', { notebookId }, undefined, { instanceId });
  }

  /**
   * The recorded cells at the end of the history's root branch (for the
   * page-open alignment of a notebook with unstable ids); empty when the
   * history does not exist yet or cannot be rebuilt.
   */
  async tip(explorationId: string): Promise<TipCell[]> {
    let outline: HistoryOutline;
    try {
      outline = await this.call('outline', {}, undefined, { explorationId });
    } catch (e) {
      if (e instanceof TransportError && e.status === 404) {
        return [];
      }
      throw e;
    }
    if (outline.highestTick < 1) {
      return [];
    }
    const result: RebuildResult = await this.call('state', {}, undefined, {
      explorationId,
      branchId: outline.rootBranchId,
      tick: String(outline.highestTick)
    });
    return result.status === 'ok'
      ? result.state.notebook.cells.map(c => ({
          cellId: c.cellId,
          type: c.cellType,
          code: c.code
        }))
      : [];
  }

  close(notebookId: string, instanceId: string): void {
    this.fire('close', { notebookId, instanceId });
  }

  ingestKeepalive(req: IngestRequest): void {
    this.fire('ingest', req);
  }

  private url(
    name: RouteName,
    params: Record<string, string>,
    query?: Record<string, string>
  ): string {
    const base = URLExt.join(
      this.settings.baseUrl,
      ROUTE_BASE,
      routePath(name, params)
    );
    return query ? base + URLExt.objectToQueryString(query) : base;
  }

  private init(
    name: RouteName,
    body: unknown,
    keepalive: boolean
  ): RequestInit {
    const init: RequestInit = { method: ROUTES[name].method };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers = { 'Content-Type': 'application/json' };
    }
    if (keepalive) {
      init.keepalive = true;
    }
    return init;
  }

  private async call<T>(
    name: RouteName,
    params: Record<string, string>,
    body?: unknown,
    query?: Record<string, string>
  ): Promise<T> {
    let response: Response;
    try {
      response = await this.makeRequest(
        this.url(name, params, query),
        this.init(name, body, false),
        this.settings
      );
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

  /** Fire and forget; never throws. */
  private fire(name: RouteName, body: unknown): void {
    try {
      void this.makeRequest(
        this.url(name, {}),
        this.init(name, body, true),
        this.settings
      ).catch(() => undefined);
    } catch {
      // the page is going away: nothing to do
    }
  }
}
