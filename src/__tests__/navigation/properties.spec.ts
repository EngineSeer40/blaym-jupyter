import * as fc from 'fast-check';
import { INavState, navigator } from '../../navigation';
import type { IStep, StepCheck } from './generate';
import { runSequence, sequenceArb } from './generate';
import { lineEvents } from './outline-gen';
import { lineTicks } from './simulated-server';

/**
 * The one rule that only generated sequences reach: looking back before a
 * branch existed shows the parent's content, stays on the branch, and stepping
 * forward enters the branch's own ticks. Checked after every step against a
 * simulated server whose answers come from an independent line model
 * (outline-gen.ts), never from src/navigation/line.ts.
 */

jest.setTimeout(300000);

function fail(message: string): never {
  throw new Error(message);
}

/** Ticks the navigator knows about, read from its outline. */
function knownTicks(s: INavState): Set<number> {
  const out = new Set<number>();
  s.outline?.branches.forEach(b => b.ownTicks.forEach(t => out.add(t)));
  return out;
}

/** The viewed (or active) line's ticks that the navigator knows about. */
function knownLine(step: IStep, branchId: string): number[] {
  const known = knownTicks(step.next);
  return lineTicks(step.server.h, branchId).filter(t => known.has(t));
}

const MOVES = new Set(['jumpTo', 'stepBack', 'stepForward', 'jumpToStart']);

/** Rule 2: going back before the branch existed shows the parent, still on the branch. */
const sharedPartKeepsBranch: StepCheck = step => {
  const { input, prev, next, result } = step;
  const h = step.server.h;
  if (MOVES.has(input.type) && !result.refusal) {
    const expected =
      input.type === 'jumpTo' ? input.branchId : prev.view.branchId;
    if (next.view.branchId !== expected) {
      fail(
        `the view moved to branch ${next.view.branchId}, expected ${expected}`
      );
    }
  }
  if (next.view.tick === undefined) {
    return;
  }
  const branch = h.branches.find(b => b.branchId === next.view.branchId);
  const owner = lineEvents(h, next.view.branchId).find(
    e => e.tick === next.view.tick
  );
  if (!branch || !owner) {
    return;
  }
  if (branch.parentTick !== undefined && next.view.tick <= branch.parentTick) {
    if (owner.branchId === branch.branchId) {
      fail("a moment before the branch point is the branch's own");
    }
  } else if (
    branch.parentTick !== undefined &&
    owner.branchId !== branch.branchId
  ) {
    fail("a moment after the branch point is not the branch's own");
  }
  if (input.type === 'stepForward' || input.type === 'stepBack') {
    const line = knownLine(step, prev.view.branchId);
    const at = prev.view.tick as number;
    const expected =
      input.type === 'stepForward'
        ? line.find(t => t > at)
        : [...line].reverse().find(t => t < at);
    const kind = input.type === 'stepForward' ? 'at_newest' : 'at_start';
    if (expected === undefined) {
      if (result.refusal?.kind !== kind) {
        fail(`expected a refusal (${kind}) at the end of the line`);
      }
    } else if (next.view.tick !== expected) {
      fail(`${input.type} went to ${next.view.tick}, expected ${expected}`);
    }
    if (
      input.type === 'stepForward' &&
      branch.parentTick === at &&
      expected !== undefined &&
      lineEvents(h, branch.branchId).find(e => e.tick === next.view.tick)
        ?.branchId !== branch.branchId
    ) {
      fail('stepping forward from the branch point did not enter the branch');
    }
  }
  if (
    input.type === 'jumpToStart' &&
    next.view.tick !== knownLine(step, next.view.branchId)[0]
  ) {
    fail('jumpToStart did not land on the first step of the line');
  }
};

describe('navigation properties', () => {
  it('the shared part is shown on the viewed branch, and stepping enters its own ticks', () => {
    fc.assert(
      fc.property(sequenceArb, seq => {
        runSequence(seq, navigator, sharedPartKeepsBranch);
      }),
      { numRuns: 50, verbose: 0 }
    );
  });
});
