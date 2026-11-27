import type { OpenGap } from 'blaym-notebook-types';
import type { EventDraft, Ids } from './types';

/**
 * The browser-side gap rules (the table is in `docs/s5-edges.md`).
 * A gap says "this recorder could not see here". The browser opens a gap for
 * three things and closes it itself at its next accepted snapshot; a gap left
 * open by an earlier instance (a lost page, another tab) is closed through
 * `close(gap)` when the server lists it in `openGaps`. A gap is never closed
 * twice, whichever route reaches it first.
 *
 * - `switched_off`: recording was switched off in this tab.
 * - `unknown`: a resync is waiting for a run to end, or the queue bound dropped
 *   edits. One gap at a time per reason.
 *
 * The start-up gap of a page that was lost is written by the server after the
 * next first snapshot (`recording/edges.py`), because only the server knows
 * whether the last session ended cleanly.
 */
export type BrowserGapReason = 'unknown' | 'switched_off';

export interface GapKeeperOptions {
  ids: Ids;
  /** Builds and queues the event; `first` puts it ahead of any held snapshot. */
  emit: (draft: EventDraft, first: boolean) => void;
}

export class GapKeeper {
  private open_ = new Map<string, BrowserGapReason>();
  private done = new Set<string>();

  constructor(private readonly o: GapKeeperOptions) {}

  get openCount(): number {
    return this.open_.size;
  }

  /** Opens a gap unless one of this reason is already open. */
  open(reason: BrowserGapReason): void {
    for (const r of this.open_.values()) {
      if (r === reason) {
        return;
      }
    }
    const gapId = this.o.ids.uuid();
    this.open_.set(gapId, reason);
    this.o.emit(
      {
        type: 'recording_gap_started',
        payload: { gapId, source: 'browser', reason }
      },
      true
    );
  }

  /** Closes every gap opened here that is still open, except `switched_off` unless `afterOn`. */
  closeAll(afterOn = true): void {
    for (const [gapId, reason] of [...this.open_]) {
      if (reason === 'switched_off' && !afterOn) {
        continue;
      }
      this.close({ gapId });
    }
  }

  /** Closes a gap by id (own, or listed by the server as left open); never twice. */
  close(gap: Pick<OpenGap, 'gapId'>): void {
    if (this.done.has(gap.gapId)) {
      return;
    }
    this.done.add(gap.gapId);
    this.open_.delete(gap.gapId);
    this.o.emit(
      { type: 'recording_gap_ended', payload: { gapId: gap.gapId } },
      false
    );
  }
}
