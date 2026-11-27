/**
 * The viewer's reads and its live stream. One `request` function goes through
 * one back end, chosen at creation: direct (`fetch`, GET, same-origin cookie)
 * or host (messages to the embedding host). No token is held here.
 */
import type {
  EventsBetweenRequest,
  EventsBetweenResponse,
  EventsSinceRequest,
  EventsSinceResponse,
  GetBlobRequest,
  HistoryAdvanced,
  HistoryOutline,
  OutlineRequest,
  RebuildResult,
  SearchRequest,
  SearchResponse,
  StateAtRequest,
  TimelineIndexRequest,
  TimelineIndexResponse
} from 'blaym-notebook-types';
import { ROUTES, routePath } from '../recording/routes';
import type { RouteName } from '../recording/routes';
import type { Cancel, Scheduler } from '../recording/types';
import { ClientError } from './host';
import type { HostChannel, RequestParams, StreamSink } from './host';

export { ClientError };

export type Backend =
  | { kind: 'direct'; base: string; fetch?: typeof fetch }
  | { kind: 'host'; channel: HostChannel };

export interface StreamHandlers {
  onAdvanced(advanced: HistoryAdvanced): void;
  onDisconnected(): void;
  onReconnected(): void;
}

export interface ClientOptions {
  backend: Backend;
  scheduler: Scheduler;
}

export interface Client {
  request(name: RouteName, params: RequestParams): Promise<unknown>;
  state(req: StateAtRequest): Promise<RebuildResult>;
  outline(req: OutlineRequest): Promise<HistoryOutline>;
  timeline(req: TimelineIndexRequest): Promise<TimelineIndexResponse>;
  eventsBetween(req: EventsBetweenRequest): Promise<EventsBetweenResponse>;
  eventsSince(req: EventsSinceRequest): Promise<EventsSinceResponse>;
  search(req: SearchRequest): Promise<SearchResponse>;
  blob(req: GetBlobRequest): Promise<ArrayBuffer>;
  /** Follow new history. Returns `stop`. */
  stream(
    explorationId: string,
    afterTick: number | undefined,
    handlers: StreamHandlers
  ): Cancel;
}

export const BACKOFF_FIRST_MS = 500;
export const BACKOFF_MAX_MS = 10000;

/** Splits the parameters into the route's path parameters and the query string. */
function split(
  name: RouteName,
  params: RequestParams
): { path: string; query: string } {
  const pathParams: Record<string, string> = {};
  const query = new URLSearchParams();
  const inPath = new Set(
    (ROUTES[name].path.match(/\{\w+\}/g) ?? []).map(k => k.slice(1, -1))
  );
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) {
      continue;
    }
    if (inPath.has(key)) {
      pathParams[key] = String(value);
    } else {
      query.set(key, Array.isArray(value) ? value.join(',') : String(value));
    }
  }
  const q = query.toString();
  return { path: routePath(name, pathParams), query: q ? `?${q}` : '' };
}

/** Splits a stream's text into frames; keeps the unfinished tail. */
export function parseFrames(buffer: string): {
  frames: { event: string; data: string }[];
  rest: string;
} {
  const parts = buffer.replace(/\r\n/g, '\n').split('\n\n');
  const rest = parts.pop() ?? '';
  const frames: { event: string; data: string }[] = [];
  for (const part of parts) {
    let event = 'message';
    const data: string[] = [];
    for (const line of part.split('\n')) {
      if (line.startsWith(':')) {
        continue;
      }
      if (line.startsWith('event:')) {
        event = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        data.push(line.slice(5).replace(/^ /, ''));
      }
    }
    if (data.length) {
      frames.push({ event, data: data.join('\n') });
    }
  }
  return { frames, rest };
}

export function createClient(o: ClientOptions): Client {
  const be = o.backend;

  async function direct(
    base: string,
    doFetch: typeof fetch,
    name: RouteName,
    params: RequestParams
  ): Promise<unknown> {
    const { path, query } = split(name, params);
    let res: Response;
    try {
      res = await doFetch(base + path + query, {
        method: 'GET',
        credentials: 'same-origin'
      });
    } catch (e) {
      throw new ClientError(String(e), 'network');
    }
    if (!res.ok) {
      throw new ClientError(`${name} failed`, 'http', res.status);
    }
    return name === 'get_blob' ? res.arrayBuffer() : res.json();
  }

  const request = (name: RouteName, params: RequestParams): Promise<unknown> =>
    be.kind === 'direct'
      ? direct(be.base, be.fetch ?? ((...a) => fetch(...a)), name, params)
      : be.channel.request(name, params);

  /** Opens one connection; the sink hears start, text and the end. */
  function open(
    explorationId: string,
    afterTick: number | undefined,
    sink: StreamSink
  ): Cancel {
    if (be.kind === 'host') {
      return be.channel.openStream(explorationId, afterTick, sink);
    }
    const doFetch = be.fetch ?? ((...a) => fetch(...a));
    const abort = new AbortController();
    const { path, query } = split('stream', { explorationId, afterTick });
    void (async () => {
      try {
        const res = await doFetch(be.base + path + query, {
          method: 'GET',
          credentials: 'same-origin',
          signal: abort.signal
        });
        if (!res.ok || !res.body) {
          throw new ClientError('stream refused', 'http', res.status);
        }
        sink.started();
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) {
            break;
          }
          sink.chunk(decoder.decode(value, { stream: true }));
        }
        sink.ended();
      } catch (e) {
        if (!abort.signal.aborted) {
          sink.ended(e);
        }
      }
    })();
    return () => abort.abort();
  }

  function stream(
    explorationId: string,
    afterTick: number | undefined,
    h: StreamHandlers
  ): Cancel {
    let stopped = false;
    let down = false;
    let attempt = 0;
    let tick = afterTick;
    let cancelOpen: Cancel | undefined;
    let cancelWait: Cancel | undefined;

    const connect = () => {
      let buffer = '';
      cancelOpen = open(explorationId, tick, {
        started: () => {
          attempt = 0;
          if (down) {
            down = false;
            h.onReconnected();
          }
        },
        chunk: text => {
          const parsed = parseFrames(buffer + text);
          buffer = parsed.rest;
          for (const f of parsed.frames) {
            if (f.event !== 'advanced') {
              continue;
            }
            const advanced = JSON.parse(f.data) as HistoryAdvanced;
            tick = advanced.tick;
            h.onAdvanced(advanced);
          }
        },
        ended: () => {
          if (stopped) {
            return;
          }
          if (!down) {
            down = true;
            h.onDisconnected();
          }
          const wait = Math.min(
            BACKOFF_FIRST_MS * 2 ** attempt,
            BACKOFF_MAX_MS
          );
          attempt += 1;
          cancelWait = o.scheduler.setTimeout(connect, wait);
        }
      });
    };
    connect();
    return () => {
      stopped = true;
      cancelOpen?.();
      cancelWait?.();
    };
  }

  const get =
    <Q extends object, R>(name: RouteName) =>
    (req: Q) =>
      request(name, req as unknown as RequestParams) as Promise<R>;

  return {
    request,
    state: get<StateAtRequest, RebuildResult>('state'),
    outline: get<OutlineRequest, HistoryOutline>('outline'),
    timeline: get<TimelineIndexRequest, TimelineIndexResponse>('timeline'),
    eventsBetween: get<EventsBetweenRequest, EventsBetweenResponse>(
      'events_between'
    ),
    eventsSince: get<EventsSinceRequest, EventsSinceResponse>('events_since'),
    search: get<SearchRequest, SearchResponse>('search'),
    blob: get<GetBlobRequest, ArrayBuffer>('get_blob'),
    stream
  };
}
