import type { CellType, RecorderEvent } from 'blaym-jupyter-types';

/**
 * The interfaces the editor-neutral recorder meets (S5, section 3). Every impure
 * thing (time, timers, ids, network) comes in through one of them; the adapter
 * of an editor supplies them. Nothing here may use the DOM.
 */

export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };

export type Cancel = () => void;
export type Disposer = () => void;

export interface Clock {
  nowMs(): number;
}

export interface Scheduler {
  setTimeout(fn: () => void, ms: number): Cancel;
}

export interface Ids {
  uuid(): string;
}

/** One cell as the editor model shows it now. */
export interface CellView {
  /** The editor's id; in a notebook with unstable ids it is a temporary id of this page open. */
  id: string;
  type: CellType;
  source: string;
  metadata: Record<string, unknown>;
  /** Raw nbformat outputs (empty for markdown and raw cells). */
  outputs: Record<string, unknown>[];
}

export interface NotebookView {
  cells(): CellView[];
  notebookMetadata(): Record<string, unknown>;
  cellIdsStable: boolean;
}

export type AuthorKind = 'ai' | 'person';
export interface AuthorSignal {
  kind: AuthorKind;
  confidence: 'known' | 'guess';
  atMs: number;
  cellId?: string;
}

export interface AuthorSignals {
  onSignal(cb: (s: AuthorSignal) => void): Disposer;
}

export interface TabElection {
  acquire(notebookId: string): Promise<'active' | 'inactive'>;
  onLost(cb: () => void): Disposer;
}

/** An event before the builder gives it its envelope. */
export interface EventDraft {
  type: RecorderEvent['type'];
  payload: Record<string, unknown>;
  /** Cell the event concerns (becomes `links.cellId`). */
  cellId?: string;
  /** Raw nbformat outputs of an inserted cell (the builder turns them into items). */
  rawOutputs?: Record<string, unknown>[];
  /** When the change was seen; defaults to the clock at build time. */
  atMs?: number;
}

export interface AuthorMark {
  hint: 'person' | 'ai' | 'outside' | 'blaym' | 'unknown';
  confidence?: 'known' | 'guess';
}
