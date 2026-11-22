import type {
  HistoryOutline,
  OutlineBranch,
  OutlineRunSpan,
  Tick
} from 'blaym-jupyter-types';

/**
 * Line arithmetic over a history outline.
 *
 * A branch's line is each ancestor's own ticks up to the next branch point,
 * then the branch's own ticks, in tick order. Everything here is a pure
 * function of the outline; there is no notebook content and no state.
 */

/** One step on a line: the tick and the branch that owns it. */
export interface ILineEntry {
  tick: Tick;
  branchId: string;
}

/** Where a position sits on a line, worked out from the outline. */
export interface ILinePosition {
  branchId: string;
  /** The tick that was asked for. */
  tick: number;
  /** The last event on the line at or before `tick`. */
  lastEventTick: number;
  /** Which event that is along the line, 1 to n. */
  branchCount: number;
}

export type OutlineProblemKind =
  | 'duplicate_branch'
  | 'unknown_root'
  | 'root_has_parent'
  | 'missing_parent'
  | 'unknown_parent'
  | 'parent_loop'
  | 'unsorted_own_ticks'
  | 'repeated_own_ticks'
  | 'run_span_backwards'
  | 'tick_claimed_twice';

/** One rule the outline breaks, in plain words. */
export interface IOutlineProblem {
  kind: OutlineProblemKind;
  branchId?: string;
  tick?: number;
  detail: string;
}

function findBranch(
  outline: HistoryOutline,
  branchId: string
): OutlineBranch | undefined {
  for (const b of outline.branches) {
    if (b.branchId === branchId) {
      return b;
    }
  }
  return undefined;
}

/** The branch and its ancestors, root first. Empty if the branch is unknown. */
function chainOf(outline: HistoryOutline, branchId: string): OutlineBranch[] {
  const chain: OutlineBranch[] = [];
  const seen: { [id: string]: boolean } = {};
  let current = findBranch(outline, branchId);
  if (!current) {
    return chain;
  }
  while (current && !seen[current.branchId]) {
    seen[current.branchId] = true;
    chain.unshift(current);
    current =
      current.parentBranchId === undefined
        ? undefined
        : findBranch(outline, current.parentBranchId);
  }
  return chain;
}

/**
 * The line of a branch: every step from the start of history to the branch's
 * newest event. Empty for an unknown branch.
 */
export function branchLine(
  outline: HistoryOutline,
  branchId: string
): ILineEntry[] {
  const chain = chainOf(outline, branchId);
  let line: ILineEntry[] = [];
  for (const branch of chain) {
    // A branch point cuts the parent's line, which may itself already be cut.
    const limit =
      branch.parentTick === undefined ? Infinity : branch.parentTick;
    line = line.filter(e => e.tick <= limit);
    for (const tick of branch.ownTicks) {
      line.push({ tick, branchId: branch.branchId });
    }
  }
  return line;
}

/** The ticks of a branch's line, in order. */
export function lineTicks(outline: HistoryOutline, branchId: string): Tick[] {
  return branchLine(outline, branchId).map(e => e.tick);
}

/** The newest tick on the line, or undefined when the line is empty. */
export function tipOf(
  outline: HistoryOutline,
  branchId: string
): Tick | undefined {
  const line = branchLine(outline, branchId);
  return line.length ? line[line.length - 1].tick : undefined;
}

/** The last tick on the line strictly before `tick`, if any. */
export function previousTick(
  outline: HistoryOutline,
  branchId: string,
  tick: number
): Tick | undefined {
  let found: Tick | undefined;
  for (const e of branchLine(outline, branchId)) {
    if (e.tick < tick) {
      found = e.tick;
    } else {
      break;
    }
  }
  return found;
}

/** The first tick on the line strictly after `tick`, if any. */
export function nextTick(
  outline: HistoryOutline,
  branchId: string,
  tick: number
): Tick | undefined {
  for (const e of branchLine(outline, branchId)) {
    if (e.tick > tick) {
      return e.tick;
    }
  }
  return undefined;
}

/**
 * Branch count and last event tick at a position. Undefined when the branch
 * is unknown or the line has no event at or before `tick`. It must equal the
 * `StatePosition` the rebuilder reports.
 */
export function positionAt(
  outline: HistoryOutline,
  branchId: string,
  tick: number
): ILinePosition | undefined {
  const line = branchLine(outline, branchId);
  let count = 0;
  for (const e of line) {
    if (e.tick <= tick) {
      count += 1;
    } else {
      break;
    }
  }
  if (count === 0) {
    return undefined;
  }
  return {
    branchId,
    tick,
    lastEventTick: line[count - 1].tick,
    branchCount: count
  };
}

/** The steps two lines have in common: the shared start of history. */
export function sharedPart(
  outline: HistoryOutline,
  branchA: string,
  branchB: string
): ILineEntry[] {
  const a = branchLine(outline, branchA);
  const b = branchLine(outline, branchB);
  const shared: ILineEntry[] = [];
  for (let i = 0; i < a.length && i < b.length; i++) {
    if (a[i].tick !== b[i].tick || a[i].branchId !== b[i].branchId) {
      break;
    }
    shared.push(a[i]);
  }
  return shared;
}

/**
 * The branch that owns the last event at or before `tick` on this line, so
 * a moment at or before a child's branch point belongs to an ancestor.
 * Undefined when there is no event at or before `tick`.
 */
export function ownerOfTick(
  outline: HistoryOutline,
  branchId: string,
  tick: number
): string | undefined {
  let owner: string | undefined;
  for (const e of branchLine(outline, branchId)) {
    if (e.tick <= tick) {
      owner = e.branchId;
    } else {
      break;
    }
  }
  return owner;
}

function isOpenAt(span: OutlineRunSpan, tick: number): boolean {
  return (
    span.startedTick <= tick &&
    (span.endedTick === undefined || span.endedTick > tick)
  );
}

/**
 * True when no run is in progress at this moment. The moment is judged at the
 * last line event at or before `tick` (the state shown there), using the spans
 * of the branch that owns that event, which may be an ancestor. A moment
 * before any line event is settled.
 */
export function isSettledAt(
  outline: HistoryOutline,
  branchId: string,
  tick: number
): boolean {
  // A tick off the line shows the state at the last line event at or before it.
  const at = positionAt(outline, branchId, tick);
  if (!at) {
    return true;
  }
  const ownerId = ownerOfTick(outline, branchId, at.lastEventTick);
  const owner =
    ownerId === undefined ? undefined : findBranch(outline, ownerId);
  if (!owner) {
    return true;
  }
  return !owner.runSpans.some(span => isOpenAt(span, at.lastEventTick));
}

/**
 * Checks the rules the JSON Schema cannot express. An empty list means the
 * outline is sound.
 */
export function checkOutline(outline: HistoryOutline): IOutlineProblem[] {
  const problems: IOutlineProblem[] = [];
  const ids: { [id: string]: boolean } = {};
  const claimedBy: { [tick: number]: string } = {};

  for (const b of outline.branches) {
    if (ids[b.branchId]) {
      problems.push({
        kind: 'duplicate_branch',
        branchId: b.branchId,
        detail: `branch ${b.branchId} is listed more than once`
      });
    }
    ids[b.branchId] = true;
  }

  if (!ids[outline.rootBranchId]) {
    problems.push({
      kind: 'unknown_root',
      branchId: outline.rootBranchId,
      detail: `the root branch ${outline.rootBranchId} is not in the outline`
    });
  }

  for (const b of outline.branches) {
    const isRoot = b.branchId === outline.rootBranchId;
    if (isRoot) {
      if (b.parentBranchId !== undefined || b.parentTick !== undefined) {
        problems.push({
          kind: 'root_has_parent',
          branchId: b.branchId,
          detail: `the root branch ${b.branchId} must not have a parent`
        });
      }
    } else if (b.parentBranchId === undefined || b.parentTick === undefined) {
      problems.push({
        kind: 'missing_parent',
        branchId: b.branchId,
        detail: `branch ${b.branchId} needs a parent branch and a parent tick`
      });
    } else if (!ids[b.parentBranchId]) {
      problems.push({
        kind: 'unknown_parent',
        branchId: b.branchId,
        detail: `branch ${b.branchId} names an unknown parent ${b.parentBranchId}`
      });
    } else {
      // Walk up; more steps than branches means a loop.
      let steps = 0;
      let cursor: OutlineBranch | undefined = b;
      while (cursor && cursor.parentBranchId !== undefined) {
        cursor = findBranch(outline, cursor.parentBranchId);
        steps += 1;
        if (steps > outline.branches.length) {
          problems.push({
            kind: 'parent_loop',
            branchId: b.branchId,
            detail: `the parents of branch ${b.branchId} lead back to itself`
          });
          break;
        }
      }
    }

    for (let i = 1; i < b.ownTicks.length; i++) {
      if (b.ownTicks[i] === b.ownTicks[i - 1]) {
        problems.push({
          kind: 'repeated_own_ticks',
          branchId: b.branchId,
          tick: b.ownTicks[i],
          detail: `branch ${b.branchId} lists tick ${b.ownTicks[i]} twice`
        });
      } else if (b.ownTicks[i] < b.ownTicks[i - 1]) {
        problems.push({
          kind: 'unsorted_own_ticks',
          branchId: b.branchId,
          tick: b.ownTicks[i],
          detail: `branch ${b.branchId} lists tick ${b.ownTicks[i]} after a higher tick`
        });
      }
    }

    for (const t of b.ownTicks) {
      const other = claimedBy[t];
      if (other !== undefined && other !== b.branchId) {
        problems.push({
          kind: 'tick_claimed_twice',
          branchId: b.branchId,
          tick: t,
          detail: `tick ${t} belongs to both ${other} and ${b.branchId}`
        });
      } else {
        claimedBy[t] = b.branchId;
      }
    }

    for (const span of b.runSpans) {
      if (span.endedTick !== undefined && span.endedTick < span.startedTick) {
        problems.push({
          kind: 'run_span_backwards',
          branchId: b.branchId,
          tick: span.startedTick,
          detail: `run ${span.runId} on ${b.branchId} ends at ${span.endedTick}, before it starts at ${span.startedTick}`
        });
      }
    }
  }
  return problems;
}
