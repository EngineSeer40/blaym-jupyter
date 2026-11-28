import type { HistoryOutline } from 'blaym-notebook-types';
import type { Client } from './client';
import { entryAt } from './context';
import type { ContextEntry } from './context';
import { appendEntries } from './modes';
import type { Session } from './session';

export interface Timelines {
  /** Brings every branch's entries up to date. At most one run goes at a time; a call that arrives meanwhile makes one more run after it. */
  sync(): Promise<void>;
  /** The entries of the viewed branch's line (ancestors up to its branch point, then its own), oldest first. */
  viewed(): ContextEntry[];
  /** The branches the outline names, for the tree's lanes; empty before the first outline. */
  branches(): HistoryOutline['branches'];
  /** The entries of the branch's own ticks only, oldest first: what the tree draws in its lane. */
  own(branchId: string): ContextEntry[];
}

export interface TimelinesOptions {
  client: Pick<Client, 'timeline' | 'outline'>;
  explorationId: string;
  session: Pick<Session, 'view' | 'activeBranchId' | 'activeTip' | 'branch'>;
}

/** Per-branch timeline entries: the active branch follows its tip live; every other outline branch is loaded once, and again only when it is new. */
export function createTimelines(o: TimelinesOptions): Timelines {
  const lines = new Map<string, ContextEntry[]>();
  let outline: HistoryOutline | undefined;
  let syncing: Promise<void> | undefined;
  let syncAgain = false;
  /** Per branch, the own ticks the outline listed when its line was last fetched, and the branch that was active then. */
  const seen = new Map<string, string>();
  let wasActive: string | undefined;

  /** Fetches the timeline index of `branchId` from `fromTick`, page by page. */
  async function fetchEntries(
    branchId: string,
    fromTick?: number
  ): Promise<ContextEntry[]> {
    const got: ContextEntry[] = [];
    for (;;) {
      const page = await o.client.timeline({
        explorationId: o.explorationId,
        branchId,
        ...(fromTick === undefined ? {} : { fromTick })
      });
      for (let i = 0; i < page.ticks.length; i++) {
        got.push(entryAt(page, i));
      }
      if (!page.truncated || page.nextTick === undefined) {
        return got;
      }
      fromTick = page.nextTick;
    }
  }

  /** Only the new entries for the active branch, or all of them for a branch not held yet or after a gap. */
  async function syncActive(branchId: string): Promise<void> {
    const held = lines.get(branchId);
    const last = held?.length ? held[held.length - 1].tick : undefined;
    if (held && last !== undefined) {
      const tip = o.session.activeTip();
      if (tip === undefined || tip <= last) {
        return;
      }
      const next = appendEntries(held, await fetchEntries(branchId, last + 1));
      if (!next.gap) {
        lines.set(branchId, next.entries);
        return;
      }
    }
    lines.set(branchId, await fetchEntries(branchId));
  }

  async function syncOnce(): Promise<void> {
    const active = o.session.activeBranchId() || (await o.session.branch());
    // A branch that has just become active may have changed while it was not followed: read it afresh.
    const switched = wasActive !== undefined && wasActive !== active;
    if (switched) {
      lines.delete(active);
    }
    wasActive = active;
    try {
      await syncActive(active);
    } catch {
      // The panel keeps what it has; the page keeps working without a timeline.
    }
    // The outline is read again only when the tip has moved past it or a branch is unknown.
    const tip = o.session.activeTip();
    const viewedId = o.session.view().branchId;
    if (
      !outline ||
      switched ||
      (tip !== undefined && tip > outline.highestTick) ||
      (viewedId && !outline.branches.some(b => b.branchId === viewedId))
    ) {
      try {
        outline = await o.client.outline({ explorationId: o.explorationId });
      } catch {
        // Tried again on the next sync.
      }
    }
    for (const b of outline?.branches ?? []) {
      const sig = `${b.ownTicks.length}:${b.ownTicks[b.ownTicks.length - 1]}`;
      // A branch other than the active one is read again when the outline's own ticks for it changed.
      if (
        !lines.has(b.branchId) ||
        (b.branchId !== active && seen.get(b.branchId) !== sig)
      ) {
        try {
          lines.set(b.branchId, await fetchEntries(b.branchId));
          seen.set(b.branchId, sig);
        } catch {
          // Tried again on the next sync.
        }
      }
    }
  }

  return {
    sync() {
      if (syncing) {
        syncAgain = true;
        return syncing;
      }
      syncing = (async () => {
        try {
          do {
            syncAgain = false;
            await syncOnce();
          } while (syncAgain);
        } finally {
          syncing = undefined;
        }
      })();
      return syncing;
    },
    viewed() {
      return lines.get(o.session.view().branchId) ?? [];
    },
    branches() {
      return outline?.branches ?? [];
    },
    own(branchId) {
      const line = lines.get(branchId) ?? [];
      const meta = outline?.branches.find(b => b.branchId === branchId);
      if (!meta) {
        return line;
      }
      const own = new Set(meta.ownTicks);
      const known = outline?.highestTick ?? 0;
      // Ticks newer than the outline were recorded after it was read: own to the branch that is growing.
      return line.filter(e => own.has(e.tick) || e.tick > known);
    }
  };
}
