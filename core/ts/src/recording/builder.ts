import type {
  Editor,
  NotebookCell,
  OutputItem,
  RecorderEvent
} from 'blaym-notebook-types';
import { applyPolicy, MetadataPolicy } from './metadata';
import type { Transport } from './transport';
import type { AuthorMark, Clock, EventDraft, Ids } from './types';
import type { RecCell } from './view';

export interface BuilderOptions {
  clock: Clock;
  ids: Ids;
  transport: Transport;
  explorationId: string;
  editor: Editor;
  /** This recorder instance (one tab). */
  originId: string;
  policy: MetadataPolicy;
}

export interface SnapshotInput {
  path: string;
  notebookMetadata: Record<string, unknown>;
  kernelName: string;
  language: string;
  cells: RecCell[];
}

const MAX_OUTPUTS_PER_REQUEST = 200;

/**
 * Makes `RecorderEvent`s: envelope fields from the injected clock and ids,
 * `authorHint` `unknown` with no confidence unless a mark says otherwise (O2),
 * and each cell's revision. Revisions never go down: source steps count up from
 * the last recorded revision, and a snapshot continues from the server's
 * `cellRevisions`, adding one only when the code changed. Outputs are never
 * built here: they go through `Transport.prepareOutputs`.
 * Builds complete in call order.
 */
export class EventBuilder {
  private revisions = new Map<string, { revision: number; code?: string }>();
  private chain: Promise<unknown> = Promise.resolve();

  constructor(private readonly o: BuilderOptions) {}

  /** The tip revision of every cell as the server reports it (never lowers a known revision). */
  noteStatus(cellRevisions: Record<string, number>): void {
    Object.keys(cellRevisions).forEach(id => {
      const known = this.revisions.get(id);
      const n = cellRevisions[id];
      if (known === undefined) {
        this.revisions.set(id, { revision: n });
      } else if (n > known.revision) {
        this.revisions.set(id, { revision: n, code: undefined });
      }
    });
  }

  /** A branch switch: the revisions are exactly the new branch's (they may go down). */
  adopt(
    cells: Array<{ cellId: string; revision: number; code: string }>
  ): void {
    this.revisions = new Map(
      cells.map(c => [c.cellId, { revision: c.revision, code: c.code }])
    );
  }

  /** The recorded code of cells at the tip (from alignment), so an unchanged cell keeps its revision. */
  noteTip(tip: Array<{ cellId: string; code: string }>): void {
    tip.forEach(t => {
      const known = this.revisions.get(t.cellId);
      if (known !== undefined) {
        known.code = t.code;
      }
    });
  }

  revisionOf(cellId: string): number | undefined {
    return this.revisions.get(cellId)?.revision;
  }

  build(draft: EventDraft, author?: AuthorMark): Promise<RecorderEvent> {
    return this.enqueue(async () => {
      const eventId = this.o.ids.uuid();
      const payload = { ...draft.payload };
      let cellRevision: number | undefined;
      switch (draft.type) {
        case 'cell_inserted':
          payload.revision = 1;
          cellRevision = 1;
          this.revisions.set(String(payload.cellId), {
            revision: 1,
            code: String(payload.code)
          });
          break;
        case 'cell_source_changed': {
          const id = String(payload.cellId);
          const known = this.revisions.get(id);
          cellRevision = (known?.revision ?? 1) + 1;
          payload.revision = cellRevision;
          this.revisions.set(id, {
            revision: cellRevision,
            code: String(payload.code)
          });
          break;
        }
        case 'run_started':
          cellRevision = Number(payload.cellRevision);
          break;
        case 'cell_deleted':
          this.revisions.delete(String(payload.cellId));
          break;
        default:
          break;
      }
      if (draft.rawOutputs && draft.rawOutputs.length > 0) {
        payload.outputs = await this.prepare(draft.rawOutputs);
      }
      return this.envelope(
        eventId,
        draft.type,
        payload,
        draft.atMs,
        author,
        draft.cellId,
        cellRevision,
        draft.runId
      );
    });
  }

  /** A whole-notebook snapshot; its outputs form the groups `snapshot:<eventId>`. */
  snapshot(input: SnapshotInput, author?: AuthorMark): Promise<RecorderEvent> {
    return this.enqueue(async () => {
      const eventId = this.o.ids.uuid();
      const next = new Map<string, { revision: number; code?: string }>();
      const cells: NotebookCell[] = [];
      for (const c of input.cells) {
        const known = this.revisions.get(c.id);
        const revision =
          known === undefined
            ? 1
            : known.code === c.code
              ? known.revision
              : known.revision + 1;
        next.set(c.id, { revision, code: c.code });
        const items = c.outputs.length > 0 ? await this.prepare(c.outputs) : [];
        cells.push({
          cellId: c.id,
          cellType: c.type,
          code: c.code,
          revision,
          metadata: applyPolicy(c.metadata, 'cell', this.o.policy).metadata,
          outputs:
            items.length > 0 ? [{ runId: `snapshot:${eventId}`, items }] : []
        });
      }
      this.revisions = next;
      return this.envelope(
        eventId,
        'notebook_snapshot',
        {
          path: input.path,
          notebookMetadata: applyPolicy(
            input.notebookMetadata,
            'notebook',
            this.o.policy
          ).metadata,
          kernelName: input.kernelName,
          language: input.language,
          cells
        },
        undefined,
        author
      );
    });
  }

  private enqueue<T>(work: () => Promise<T>): Promise<T> {
    const p = this.chain.then(work);
    this.chain = p.catch(() => undefined);
    return p;
  }

  private async prepare(
    outputs: Record<string, unknown>[]
  ): Promise<OutputItem[]> {
    const items: OutputItem[] = [];
    for (let i = 0; i < outputs.length; i += MAX_OUTPUTS_PER_REQUEST) {
      const chunk = outputs.slice(i, i + MAX_OUTPUTS_PER_REQUEST);
      const res = await this.o.transport.prepareOutputs({
        explorationId: this.o.explorationId,
        outputs: chunk
      });
      if (res.items.length !== chunk.length) {
        throw new Error('prepareOutputs answered a different number of items');
      }
      items.push(...res.items);
    }
    return items;
  }

  private envelope(
    eventId: string,
    type: RecorderEvent['type'],
    payload: Record<string, unknown>,
    atMs: number | undefined,
    author: AuthorMark | undefined,
    cellId?: string,
    cellRevision?: number,
    runId?: string
  ): RecorderEvent {
    const hint = author?.hint ?? 'unknown';
    const event: Record<string, unknown> = {
      schemaVersion: 2,
      eventId,
      explorationId: this.o.explorationId,
      wallTimeMs: atMs ?? this.o.clock.nowMs(),
      userId: 'local',
      workspaceId: 'local',
      originId: this.o.originId,
      editor: this.o.editor,
      authorHint: hint,
      type,
      payload
    };
    if (hint !== 'unknown' && author?.confidence) {
      event.authorConfidence = author.confidence;
    }
    if (cellId !== undefined) {
      const links: Record<string, unknown> = { cellId };
      if (cellRevision !== undefined) {
        links.cellRevision = cellRevision;
      }
      if (runId !== undefined) {
        links.runId = runId;
      }
      event.links = links;
    }
    return event as unknown as RecorderEvent;
  }
}
