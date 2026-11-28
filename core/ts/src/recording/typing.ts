import type { AuthorMark, Clock, EventDraft, Scheduler, Cancel } from './types';

export interface TypingOptions {
  scheduler: Scheduler;
  clock: Clock;
  /** A finished step. `author` comes from `mark`. */
  emit: (draft: EventDraft, author: AuthorMark) => void;
  /** Marks a step seen from `fromMs` to `toMs` (the author marker); default: unknown. */
  mark?: (cellId: string, fromMs: number, toMs: number) => AuthorMark;
  pauseMs?: number;
  maxMs?: number;
}

interface Open {
  code: string;
  firstAt: number;
  lastAt: number;
  pause?: Cancel;
  max?: Cancel;
}

/**
 * Groups per-keystroke edits into steps: a step ends after `pauseMs`
 * (800) of quiet, after `maxMs` (5 s) of continuous typing, and at once at
 * `beforeRun`, `flushCell` (structure change, save of that cell), `flushAll`
 * (save, hide, takeover, shutdown) and `boundary` (an author signal). A step is
 * one `cell_source_changed` draft with the cell's whole new code; the builder
 * gives it its revision. A step whose code equals the last recorded code
 * (typed, then undone) is not emitted.
 */
export class TypingGrouper {
  private open = new Map<string, Open>();
  private recorded = new Map<string, string>();
  private readonly pauseMs: number;
  private readonly maxMs: number;

  constructor(private readonly o: TypingOptions) {
    this.pauseMs = o.pauseMs ?? 800;
    this.maxMs = o.maxMs ?? 5000;
  }

  /** The code the history holds for a cell (after an insert, snapshot or flushed step). */
  baseline(cellId: string, code: string): void {
    this.recorded.set(cellId, code);
  }

  /** Drop every step in progress and take `cells` as the recorded code (a branch switch). */
  reset(cells: Array<{ id: string; code: string }>): void {
    this.open.forEach(s => this.cancelTimers(s));
    this.open.clear();
    this.recorded.clear();
    cells.forEach(c => this.recorded.set(c.id, c.code));
  }

  forget(cellId: string): void {
    this.cancelTimers(this.open.get(cellId));
    this.open.delete(cellId);
    this.recorded.delete(cellId);
  }

  /** The cell's code is now `code` (latest wins inside a step). */
  edit(cellId: string, code: string): void {
    const now = this.o.clock.nowMs();
    let s = this.open.get(cellId);
    if (s === undefined) {
      s = { code, firstAt: now, lastAt: now };
      this.open.set(cellId, s);
      s.max = this.o.scheduler.setTimeout(
        () => this.flushCell(cellId),
        this.maxMs
      );
    }
    s.code = code;
    s.lastAt = now;
    if (s.pause) {
      s.pause();
    }
    s.pause = this.o.scheduler.setTimeout(
      () => this.flushCell(cellId),
      this.pauseMs
    );
  }

  /** The run's code is about to be sent: the version that runs is always kept. */
  beforeRun(cellId: string): void {
    this.flushCell(cellId);
  }

  /** Author-signal boundary: one cell, or all when no cell is named. */
  boundary(cellId?: string): void {
    if (cellId === undefined) {
      this.flushAll();
    } else {
      this.flushCell(cellId);
    }
  }

  flushCell(cellId: string): void {
    const s = this.open.get(cellId);
    if (s === undefined) {
      return;
    }
    this.cancelTimers(s);
    this.open.delete(cellId);
    if (this.recorded.get(cellId) === s.code) {
      return;
    }
    this.recorded.set(cellId, s.code);
    const author = this.o.mark
      ? this.o.mark(cellId, s.firstAt, s.lastAt)
      : ({ hint: 'unknown' } as AuthorMark);
    this.o.emit(
      {
        type: 'cell_source_changed',
        payload: { cellId, code: s.code },
        cellId,
        atMs: s.lastAt
      },
      author
    );
  }

  flushAll(): void {
    Array.from(this.open.keys()).forEach(id => this.flushCell(id));
  }

  hasOpen(cellId?: string): boolean {
    return cellId === undefined ? this.open.size > 0 : this.open.has(cellId);
  }

  private cancelTimers(s?: Open): void {
    if (s) {
      if (s.pause) {
        s.pause();
      }
      if (s.max) {
        s.max();
      }
    }
  }
}
