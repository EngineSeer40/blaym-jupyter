/**
 * Who made each cell's latest change: pure, from the timeline index. Only
 * entries that change a cell's content or place count (edit, insert, type
 * change, outside change); runs and outputs do not. A cell with no such entry
 * stays unknown: a person is never guessed.
 */
import type { AuthorConfidence, AuthorHint } from 'blaym-notebook-types';
import type { ContextEntry } from './context';

export interface CellAuthor {
  author: AuthorHint;
  confidence: AuthorConfidence | null;
  /** The chat turn that made the change, when it is known (S7); nothing sets it yet. */
  chatTurnId?: string;
}

const EDIT_LIKE = new Set(['edit', 'insert', 'type', 'outside']);

export const UNKNOWN_AUTHOR: CellAuthor = {
  author: 'unknown',
  confidence: null
};

/** The author of each cell named by an edit-like entry at or before `tick`. */
export function authors(
  entries: readonly ContextEntry[],
  tick: number
): Map<string, CellAuthor> {
  const out = new Map<string, CellAuthor>();
  for (const e of entries) {
    if (e.tick > tick) {
      break;
    }
    if (e.cellId !== null && EDIT_LIKE.has(e.kind)) {
      // A person is never guessed: a guessed person reads as unknown.
      out.set(
        e.cellId,
        e.author === 'person' && e.confidence === 'guess'
          ? UNKNOWN_AUTHOR
          : { author: e.author, confidence: e.confidence }
      );
    }
  }
  return out;
}
