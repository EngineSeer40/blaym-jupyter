import type {
  IngestRequest,
  IngestResponse,
  NotebookRegistration,
  NotebookStatus,
  PrepareOutputsRequest,
  PrepareOutputsResponse
} from 'blaym-jupyter-types';

/** The server API as the recorder uses it. A refusal is an outcome, a network failure is a rejection. */
export interface Transport {
  ingest(req: IngestRequest): Promise<IngestResponse>;
  prepareOutputs(req: PrepareOutputsRequest): Promise<PrepareOutputsResponse>;
  register(req: NotebookRegistration): Promise<NotebookStatus>;
  /** The heartbeat. */
  status(notebookId: string, instanceId: string): Promise<NotebookStatus>;
  /** A clean end; the adapter implements it with a keepalive request. Never throws. */
  close(notebookId: string, instanceId: string): void;
  /**
   * Optional: like `ingest` but allowed to outlive the page (keepalive). Used
   * for the last batch when the page hides.
   */
  ingestKeepalive?(req: IngestRequest): void;
}

/**
 * What an adapter's transport rejects with. A refusal by the service is never
 * an error: it is an outcome in the response. `network` (no answer) and a 5xx
 * status are retried by the outbox; 401 and 403 pause it; other statuses are
 * reported and answered with a resync.
 */
export class TransportError extends Error {
  constructor(
    message: string,
    readonly kind: 'network' | 'http',
    readonly status?: number
  ) {
    super(message);
    this.name = 'TransportError';
  }
}
