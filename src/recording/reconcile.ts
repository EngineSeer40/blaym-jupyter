import type { CellType } from 'blaym-jupyter-types';
import type { CellIdentity } from './ids';
import { MetadataPolicy, stableStringify } from './metadata';
import type { EventDraft, NotebookView } from './types';
import { readView } from './view';

export type ResyncReason = 'not_started' | 'replaced' | 'duplicate_ids';

export type ReconcileResult =
  | { kind: 'resync'; reason: ResyncReason }
  | {
      kind: 'changes';
      /** Structure and metadata events, in order. */
      drafts: EventDraft[];
      /** Cells whose code now differs from what was last seen: for the typing grouper. */
      edits: Array<{ cellId: string; code: string }>;
      /** Cells the structure events concern: flush their typing before the drafts. */
      flushBefore: string[];
    };

interface Known {
  id: string;
  type: CellType;
  code: string;
  mdKey: string;
}

/** Indexes (into `seq`) of one longest strictly increasing subsequence. */
function lisIndexes(seq: number[]): Set<number> {
  const best = new Array<number>(seq.length).fill(1);
  const prev = new Array<number>(seq.length).fill(-1);
  let end = -1;
  for (let i = 0; i < seq.length; i++) {
    for (let j = 0; j < i; j++) {
      if (seq[j] < seq[i] && best[j] + 1 > best[i]) {
        best[i] = best[j] + 1;
        prev[i] = j;
      }
    }
    if (end < 0 || best[i] > best[end]) {
      end = i;
    }
  }
  const keep = new Set<number>();
  for (let i = end; i >= 0; i = prev[i]) {
    keep.add(i);
  }
  return keep;
}

/**
 * Keeps the last recorded model of the notebook and, given the editor's view
 * after any change, returns what changed (S5-K). A move or a type change is
 * seen as the same cell id with a new place or type, never as a delete plus an
 * insert. Whole-notebook changes give `resync`: the owner then sends a
 * snapshot and calls `reset`. Outputs alone are never a change: the server
 * records them from the kernel.
 */
export class Reconciler {
  private model: Known[] | null = null;
  private notebookKey = '{}';

  constructor(private readonly policy: MetadataPolicy) {}

  started(): boolean {
    return this.model !== null;
  }

  /** The recorded model is now what the view shows (after a snapshot was queued). */
  reset(view: NotebookView, identity: CellIdentity): void {
    const v = readView(view, identity, this.policy);
    this.model = v.cells.map(c => ({
      id: c.id,
      type: c.type,
      code: c.code,
      mdKey: stableStringify(c.metadata)
    }));
    this.notebookKey = stableStringify(v.notebookMetadata);
  }

  reconcile(view: NotebookView, identity: CellIdentity): ReconcileResult {
    if (this.model === null) {
      return { kind: 'resync', reason: 'not_started' };
    }
    const { cells, notebookMetadata } = readView(view, identity, this.policy);
    const prev = this.model;
    const prevById = new Map(prev.map(k => [k.id, k]));
    const curIds = new Set(cells.map(c => c.id));
    if (curIds.size !== cells.length) {
      return { kind: 'resync', reason: 'duplicate_ids' };
    }
    const common = cells.filter(c => prevById.has(c.id));
    if (prev.length > 0 && cells.length > 0 && common.length === 0) {
      return { kind: 'resync', reason: 'replaced' };
    }

    const drafts: EventDraft[] = [];
    const flush = new Set<string>();
    const edits: Array<{ cellId: string; code: string }> = [];

    prev
      .filter(k => !curIds.has(k.id))
      .forEach(k => {
        flush.add(k.id);
        drafts.push({
          type: 'cell_deleted',
          payload: { cellId: k.id },
          cellId: k.id
        });
      });

    common.forEach(c => {
      const p = prevById.get(c.id) as Known;
      if (p.type !== c.type) {
        flush.add(c.id);
        drafts.push({
          type: 'cell_type_changed',
          payload: { cellId: c.id, cellType: c.type },
          cellId: c.id
        });
      }
    });

    // Moves: cells outside one longest in-order run each move once, to just after
    // their predecessor among the common cells; then the new cells are inserted.
    const work = prev.filter(k => curIds.has(k.id)).map(k => k.id);
    const order = common.map(c => c.id);
    const stay = lisIndexes(order.map(id => work.indexOf(id)));
    order.forEach((id, k) => {
      if (stay.has(k)) {
        return;
      }
      const from = work.indexOf(id);
      work.splice(from, 1);
      const to = k === 0 ? 0 : work.indexOf(order[k - 1]) + 1;
      work.splice(to, 0, id);
      if (from !== to) {
        flush.add(id);
        drafts.push({
          type: 'cell_moved',
          payload: { cellId: id, fromIndex: from, toIndex: to },
          cellId: id
        });
      }
    });

    cells.forEach((c, index) => {
      if (prevById.has(c.id)) {
        return;
      }
      const payload: Record<string, unknown> = {
        cellId: c.id,
        index,
        cellType: c.type,
        code: c.code,
        revision: 1
      };
      if (Object.keys(c.metadata).length > 0) {
        payload.metadata = c.metadata;
      }
      drafts.push({
        type: 'cell_inserted',
        payload,
        cellId: c.id,
        rawOutputs: c.outputs.length > 0 ? c.outputs : undefined
      });
    });

    common.forEach(c => {
      const p = prevById.get(c.id) as Known;
      const key = stableStringify(c.metadata);
      if (key !== p.mdKey) {
        drafts.push({
          type: 'cell_metadata_changed',
          payload: { cellId: c.id, metadata: c.metadata },
          cellId: c.id
        });
      }
      if (c.code !== p.code) {
        edits.push({ cellId: c.id, code: c.code });
      }
    });

    const nbKey = stableStringify(notebookMetadata);
    if (nbKey !== this.notebookKey) {
      drafts.push({
        type: 'notebook_metadata_changed',
        payload: { metadata: notebookMetadata }
      });
    }

    this.model = cells.map(c => ({
      id: c.id,
      type: c.type,
      code: c.code,
      mdKey: stableStringify(c.metadata)
    }));
    this.notebookKey = nbKey;
    return { kind: 'changes', drafts, edits, flushBefore: Array.from(flush) };
  }
}
