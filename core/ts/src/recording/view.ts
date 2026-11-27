import type { CellType } from 'blaym-notebook-types';
import type { CellIdentity } from './ids';
import { applyPolicy, MetadataPolicy } from './metadata';
import type { NotebookView } from './types';

/** A cell as the recorder reasons about it: Blaym id, policy-applied metadata. */
export interface RecCell {
  id: string;
  modelId: string;
  type: CellType;
  code: string;
  metadata: Record<string, unknown>;
  outputs: Record<string, unknown>[];
}

export interface RecView {
  cells: RecCell[];
  notebookMetadata: Record<string, unknown>;
}

/** Read the editor's view once, translating ids and applying the metadata policy. */
export function readView(
  view: NotebookView,
  identity: CellIdentity,
  policy: MetadataPolicy
): RecView {
  return {
    cells: view.cells().map(c => ({
      id: identity.blaymId(c.id),
      modelId: c.id,
      type: c.type,
      code: c.source,
      metadata: applyPolicy(c.metadata, 'cell', policy).metadata,
      outputs: c.type === 'code' ? c.outputs : []
    })),
    notebookMetadata: applyPolicy(view.notebookMetadata(), 'notebook', policy)
      .metadata
  };
}
