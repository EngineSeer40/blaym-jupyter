import type {
  AuthorMark,
  AuthorSignal,
  AuthorSignals,
  Clock,
  Disposer,
  Scheduler
} from './types';

export interface AuthorOptions {
  signals: AuthorSignals;
  clock: Clock;
  scheduler: Scheduler;
  /** Called to end the open typing step of a cell (or all cells when no id). */
  boundary: (cellId?: string) => void;
  /** Half width of the match window around a change; default 250 ms. */
  windowMs?: number;
  /** How long a signal is kept for late matching; default 60 s. */
  keepMs?: number;
}

/**
 * Authorship marking, retroactive and behind an interface. A change has
 * `authorHint: unknown` and no confidence unless a signal lies within
 * `[t - window, t + window]` of it; then it carries the signal's kind and
 * confidence. A signal forces a step boundary when it arrives and again once
 * its window has passed, so a person's typing never merges into an AI step.
 */
export class AuthorMarker {
  private signals: AuthorSignal[] = [];
  private readonly windowMs: number;
  private readonly keepMs: number;
  private readonly dispose: Disposer;

  constructor(private readonly o: AuthorOptions) {
    this.windowMs = o.windowMs ?? 250;
    this.keepMs = o.keepMs ?? 60000;
    this.dispose = o.signals.onSignal(s => this.receive(s));
  }

  close(): void {
    this.dispose();
  }

  /** The mark for a change seen from `fromMs` to `toMs` in a cell (both equal for a point change). */
  mark(cellId: string | undefined, fromMs: number, toMs: number): AuthorMark {
    let best: AuthorSignal | undefined;
    for (const s of this.signals) {
      if (
        s.cellId !== undefined &&
        cellId !== undefined &&
        s.cellId !== cellId
      ) {
        continue;
      }
      if (s.atMs < fromMs - this.windowMs || s.atMs > toMs + this.windowMs) {
        continue;
      }
      if (
        best === undefined ||
        (best.confidence === 'guess' && s.confidence === 'known')
      ) {
        best = s;
      }
    }
    return best === undefined
      ? { hint: 'unknown' }
      : { hint: best.kind, confidence: best.confidence };
  }

  private receive(s: AuthorSignal): void {
    const now = this.o.clock.nowMs();
    this.signals = this.signals.filter(x => now - x.atMs <= this.keepMs);
    this.signals.push(s);
    this.o.boundary(s.cellId);
    this.o.scheduler.setTimeout(() => this.o.boundary(s.cellId), this.windowMs);
  }
}
