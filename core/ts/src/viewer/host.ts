/**
 * The message protocol between the viewer page and the host that embeds it
 * (a JupyterLab panel, a VS Code webview). The page talks only to
 * `window.parent`, or to an injected port in tests. No token is ever sent or
 * asked for: the host signs requests in itself.
 */
import type { Cancel, Scheduler } from '../recording/types';

/** What a failed read rejects with, in the style of the recorder's `TransportError`. */
export class ClientError extends Error {
  constructor(
    message: string,
    readonly kind: 'network' | 'http' | 'host' | 'timeout',
    readonly status?: number
  ) {
    super(message);
    this.name = 'ClientError';
  }
}

export interface HostMessageEvent {
  data: unknown;
  origin: string;
  source: unknown;
}

/** A way to post to the host and hear from it. */
export interface HostPort {
  post(message: unknown): void;
  /** Returns a function that stops listening. */
  listen(fn: (ev: HostMessageEvent) => void): () => void;
}

/**
 * The port over `window.parent`. Messages from any other source are dropped;
 * when `origin` is given (the direct mode: the page's own origin) messages
 * from any other origin are dropped as well and posts are addressed to it.
 */
export function windowPort(origin?: string): HostPort {
  return {
    post: message => window.parent.postMessage(message, origin ?? '*'),
    listen: fn => {
      const on = (ev: MessageEvent) => {
        if (ev.source !== window.parent) {
          return;
        }
        if (origin !== undefined && ev.origin !== origin) {
          return;
        }
        fn({ data: ev.data, origin: ev.origin, source: ev.source });
      };
      window.addEventListener('message', on);
      return () => window.removeEventListener('message', on);
    }
  };
}

export type RequestParams = Record<
  string,
  string | number | string[] | undefined
>;

export interface RequestMessage {
  type: 'blaym:request';
  id: string;
  name: string;
  params: RequestParams;
}

export interface ResponseMessage {
  type: 'blaym:response';
  id: string;
  ok: boolean;
  body?: unknown;
  error?: { message: string; status?: number };
}

export type StreamPhase = 'start' | 'frame' | 'end';

/** Page to host: open or close a stream. */
export interface StreamControlMessage {
  type: 'blaym:stream';
  streamId: string;
  phase: 'open' | 'close';
  explorationId?: string;
  afterTick?: number;
}

/** Host to page: the stream began, carries raw frame text, or ended (with an error text if it failed). */
export interface StreamRelayMessage {
  type: 'blaym:stream';
  streamId: string;
  phase: StreamPhase;
  data?: string;
  error?: string;
}

/** What the page hears while a stream is open. */
export interface StreamSink {
  started(): void;
  chunk(text: string): void;
  ended(error?: unknown): void;
}

/** Host to page: the host theme. `values` holds `--jp-*` names; a missing one falls back to the kind's fallback. */
export interface ThemeMessage {
  type: 'blaym:theme';
  kind: 'light' | 'dark';
  values?: Record<string, string>;
}

/** The host's optional calls into the live notebook. Sent as messages. */
export interface HostLive {
  scrollToCell(cellId: string): void;
  decorateCell(cellId: string, mark: string): void;
}

export interface HostChannel {
  request(name: string, params: RequestParams): Promise<unknown>;
  openStream(
    explorationId: string,
    afterTick: number | undefined,
    sink: StreamSink
  ): Cancel;
  live: HostLive;
}

export interface HostChannelOptions {
  port: HostPort;
  scheduler: Scheduler;
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 15000;

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null;
}

export function createHostChannel(o: HostChannelOptions): HostChannel {
  const timeoutMs = o.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const pending = new Map<string, (m: ResponseMessage) => void>();
  const streams = new Map<string, StreamSink>();
  let next = 0;
  const newId = (): string => `bv-${++next}`;

  o.port.listen(ev => {
    const m = ev.data;
    if (!isObject(m)) {
      return;
    }
    if (m.type === 'blaym:response' && typeof m.id === 'string') {
      pending.get(m.id)?.(m as unknown as ResponseMessage);
    } else if (m.type === 'blaym:stream' && typeof m.streamId === 'string') {
      const sink = streams.get(m.streamId);
      const relay = m as unknown as StreamRelayMessage;
      if (!sink) {
        return;
      }
      if (relay.phase === 'start') {
        sink.started();
      } else if (relay.phase === 'frame') {
        sink.chunk(relay.data ?? '');
      } else if (relay.phase === 'end') {
        streams.delete(relay.streamId);
        sink.ended(
          relay.error === undefined ? undefined : new Error(relay.error)
        );
      }
    }
  });

  const live = (call: string, args: unknown[]) =>
    o.port.post({ type: 'blaym:live', call, args });

  return {
    request(name, params) {
      return new Promise((resolve, reject) => {
        const id = newId();
        const cancel = o.scheduler.setTimeout(() => {
          pending.delete(id);
          reject(new ClientError(`host did not answer ${name}`, 'timeout'));
        }, timeoutMs);
        pending.set(id, reply => {
          pending.delete(id);
          cancel();
          if (reply.ok) {
            resolve(reply.body);
          } else {
            reject(
              new ClientError(
                reply.error?.message ?? `host refused ${name}`,
                'host',
                reply.error?.status
              )
            );
          }
        });
        const message: RequestMessage = {
          type: 'blaym:request',
          id,
          name,
          params
        };
        o.port.post(message);
      });
    },
    openStream(explorationId, afterTick, sink) {
      const streamId = newId();
      streams.set(streamId, sink);
      const open: StreamControlMessage = {
        type: 'blaym:stream',
        streamId,
        phase: 'open',
        explorationId,
        afterTick
      };
      o.port.post(open);
      return () => {
        if (streams.delete(streamId)) {
          const close: StreamControlMessage = {
            type: 'blaym:stream',
            streamId,
            phase: 'close'
          };
          o.port.post(close);
        }
      };
    },
    live: {
      scrollToCell: cellId => live('scrollToCell', [cellId]),
      decorateCell: (cellId, mark) => live('decorateCell', [cellId, mark])
    }
  };
}

export type { Cancel };
