import * as fc from 'fast-check';
import { INavState, navigator } from '../../navigation';
import type { Navigate, IStep, StepCheck } from './generate';
import { isDeepStrictEqual, runSequence, sequenceArb } from './generate';
import { lineEvents, outlineOfHistory } from './outline-gen';
import { fcParams, NUM_RUNS, PROFILE } from './profile';
import { lineTicks, truncatedAt } from './simulated-server';
import { openRunsAt } from './outline-gen';

/**
 * Every navigation rule of S3b as a property, checked after every step of
 * long generated sequences against a simulated server whose answers come from
 * an independent line model (outline-gen.ts), never from src/navigation/line.ts.
 * Profile: NAV_PROFILE=default (300 runs per property) or deep (1,000).
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

/** Rule 1: a shown position always lies on its branch's line. */
const shownOnLine: StepCheck = step => {
  const { view } = step.next;
  const h = step.server.h;
  if (view.tick !== undefined) {
    if (!lineTicks(h, view.branchId).includes(view.tick)) {
      fail(`view ${view.branchId}@${view.tick} is not on that branch's line`);
    }
  }
  if (view.shown) {
    const { branchId, tick } = view.shown;
    if (!lineTicks(h, branchId).includes(tick)) {
      fail(`shown answer ${branchId}@${tick} is not on that branch's line`);
    }
  }
};

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

/** Rule 3: only a switched or branchCreated answer changes the active branch. */
const activeOnlyByAnswers: StepCheck = step => {
  const { input, prev, next, result } = step;
  if (input.type !== 'switched' && input.type !== 'branchCreated') {
    if (next.active.branchId !== prev.active.branchId) {
      fail(`${input.type} changed the active branch`);
    }
    return;
  }
  const target =
    input.type === 'switched' ? input.branchId : input.branch.branchId;
  if (result.refusal) {
    if (next.active.branchId !== prev.active.branchId) {
      fail('a refused answer changed the active branch');
    }
  } else if (next.active.branchId !== target) {
    fail(`the answer for ${target} made ${next.active.branchId} active`);
  }
};

/** Rule 4: no answer to a superseded request is shown; a current answer fits the view. */
const noSupersededAnswer: StepCheck = step => {
  const { input, prev, next, latestIssued } = step;
  const { view } = next;
  if (view.shown !== prev.view.shown) {
    if (input.type !== 'stateReceived') {
      fail(`${input.type} changed the shown answer`);
    }
    if (input.requestNo !== latestIssued) {
      fail(
        `showed the answer to request ${input.requestNo}, but request ${latestIssued} is the latest`
      );
    }
    if (
      view.shown?.requestNo !== input.requestNo ||
      view.shown?.result !== input.result
    ) {
      fail('the shown answer is not the one delivered');
    }
  }
  if (view.awaiting !== undefined && view.awaiting !== latestIssued) {
    fail(
      `awaiting request ${view.awaiting}, but ${latestIssued} is the latest`
    );
  }
  if (view.freshness === 'current') {
    const { shown } = view;
    if (
      !shown ||
      shown.branchId !== view.branchId ||
      shown.tick !== view.tick
    ) {
      fail('a current view shows an answer for another position');
    }
    if (shown.result.status === 'ok') {
      const p = shown.result.state.position;
      if (p.branchId !== view.branchId || p.tick !== view.tick) {
        fail(
          `the shown state is at ${p.branchId}@${p.tick}, the view is at ${view.branchId}@${view.tick}`
        );
      }
    }
  }
};

/** Rule 5: back to now lands on the active tip; following means being at the tip. */
const backToNowIsCurrent: StepCheck = step => {
  const { next, backToNowArmed } = step;
  const { view, active } = next;
  if (view.mode === 'following') {
    if (view.branchId !== active.branchId || view.tick !== active.tip) {
      fail(
        `following, but the view is at ${view.branchId}@${view.tick} and the tip is ${active.branchId}@${active.tip}`
      );
    }
  }
  if (next.outline) {
    const line = knownLine(step, active.branchId);
    if (active.tip !== line[line.length - 1]) {
      fail(
        `the active tip ${active.tip} is not the newest known tick ${line[line.length - 1]}`
      );
    }
  }
  if (backToNowArmed) {
    if (view.mode !== 'following') {
      fail('after back to now the view is not following');
    }
    if (view.freshness === 'current') {
      const { shown } = view;
      if (
        !shown ||
        shown.branchId !== active.branchId ||
        shown.tick !== active.tip
      ) {
        fail(
          `the first current answer after back to now is ${shown?.branchId}@${shown?.tick}, the tip is ${active.branchId}@${active.tip}`
        );
      }
    }
  }
};

/** Rule 6: no branch or switch request for an unsettled moment, or while one is pending. */
const noUnsettledBranching: StepCheck = step => {
  const { input, prev, result } = step;
  const h = step.server.h;
  for (const r of result.requests) {
    if (r.type !== 'createBranch' && r.type !== 'switchBranch') {
      continue;
    }
    if (prev.pending) {
      fail(`${r.type} was asked while another change was pending`);
    }
    const [branchId, tick] =
      r.type === 'createBranch'
        ? [r.fromBranchId, r.fromTick]
        : [prev.active.branchId, prev.active.tip as number];
    const open = openRunsAt(h, branchId, tick);
    if (open.length) {
      fail(
        `${r.type} was asked at ${branchId}@${tick} with runs open: ${open}`
      );
    }
  }
  if (result.refusal?.kind === 'run_in_progress') {
    const [branchId, tick] =
      input.type === 'continueFromHere'
        ? [prev.view.branchId, prev.view.tick as number]
        : [prev.active.branchId, prev.active.tip as number];
    if (!openRunsAt(h, branchId, tick).length) {
      fail(
        `refused for a run in progress, but none is open at ${branchId}@${tick}`
      );
    }
  }
};

/** Rule 7: looking back, new changes are counted and the view does not move. */
const lookingBackIsStable: StepCheck = step => {
  const { input, prev, next, lookBackFrom } = step;
  const h = step.server.h;
  const passive = [
    'historyAdvanced',
    'outlineReceived',
    'disconnected',
    'reconnected',
    'stateReceived'
  ];
  if (
    prev.view.mode === 'lookingBack' &&
    next.view.mode === 'lookingBack' &&
    passive.includes(input.type)
  ) {
    if (
      prev.view.branchId !== next.view.branchId ||
      prev.view.tick !== next.view.tick
    ) {
      fail(
        `${input.type} moved the view from ${prev.view.branchId}@${prev.view.tick} to ${next.view.branchId}@${next.view.tick}`
      );
    }
  }
  let expected = 0;
  if (next.view.mode === 'lookingBack') {
    const known = knownTicks(next);
    expected = lineTicks(h, next.active.branchId).filter(
      t => t > lookBackFrom && known.has(t)
    ).length;
  }
  if (next.newSinceLookingBack !== expected) {
    fail(
      `the new-changes count is ${next.newSinceLookingBack}, expected ${expected}`
    );
  }
};

/** The navigator's outline agrees with the independent model once it has no gaps. */
const outlineAgrees: StepCheck = step => {
  const o = step.next.outline;
  if (!o) {
    return;
  }
  const known = knownTicks(step.next);
  if (Math.max(...known) !== o.highestTick) {
    return;
  }
  const expected = outlineOfHistory(truncatedAt(step.server.h, o.highestTick));
  const ids = (x: typeof o) => x.branches.map(b => b.branchId).sort();
  if (!isDeepStrictEqual(ids(o), ids(expected))) {
    fail(`branches ${ids(o)} differ from the model's ${ids(expected)}`);
  }
  for (const b of o.branches) {
    const m = expected.branches.find(x => x.branchId === b.branchId)!;
    const spans = (s: typeof b) =>
      s.runSpans
        .map(r => `${r.runId}:${r.startedTick}-${r.endedTick ?? ''}`)
        .sort();
    if (
      !isDeepStrictEqual(b.ownTicks, m.ownTicks) ||
      b.parentBranchId !== m.parentBranchId ||
      b.parentTick !== m.parentTick ||
      !isDeepStrictEqual(spans(b), spans(m))
    ) {
      fail(`branch ${b.branchId} differs from the model`);
    }
  }
};

/** A failed switch or branch leaves everything as it was, apart from the pending marker. */
const failedSwitchRestores: StepCheck = step => {
  const { input, prev, next, result } = step;
  if (input.type === 'switchFailed' && prev.pending) {
    const before = { ...prev };
    delete before.pending;
    if (!isDeepStrictEqual(next, before)) {
      fail('a failed switch changed the state');
    }
    if (result.refusal?.kind !== 'switch_failed' || result.requests.length) {
      fail('a failed switch did not report the failure plainly');
    }
  }
  if (
    (input.type === 'switched' || input.type === 'branchCreated') &&
    !result.refusal &&
    next.pending
  ) {
    fail('an answered change is still pending');
  }
};

/**
 * A switch or branch answer that arrives when nothing is pending (a duplicate,
 * or a late one) changes nothing and shows nothing.
 */
const unexpectedAnswerIsIgnored: StepCheck = step => {
  const { input, prev, next, result } = step;
  if (
    (input.type === 'switched' || input.type === 'branchCreated') &&
    !prev.pending
  ) {
    if (!result.refusal || result.requests.length) {
      fail(`${input.type} with nothing pending was not refused`);
    }
    if (!isDeepStrictEqual(next, prev)) {
      fail(`${input.type} with nothing pending changed the state`);
    }
  }
};

function runs(
  check: StepCheck,
  navigate: Navigate = navigator,
  deterministic = false
) {
  return fc.property(sequenceArb, seq => {
    runSequence(seq, navigate, check, deterministic);
  });
}

const PROPERTIES: Array<[string, StepCheck]> = [
  ['1. a shown position always lies on its branch line', shownOnLine],
  [
    '2. the shared part is shown on the viewed branch, and stepping enters its own ticks',
    sharedPartKeepsBranch
  ],
  [
    '3. only switched and branchCreated answers change the active branch',
    activeOnlyByAnswers
  ],
  [
    '4. a superseded answer is never shown; a current answer fits the view',
    noSupersededAnswer
  ],
  ['5. back to now lands on the active tip, current', backToNowIsCurrent],
  [
    '6. no branch or switch for an unsettled moment or while pending',
    noUnsettledBranching
  ],
  [
    '7. looking back: the view stays and the count is exact, also after reconnect',
    lookingBackIsStable
  ],
  ['8. the navigator outline agrees with the independent model', outlineAgrees],
  [
    '9. a failed switch restores the state; answers clear pending',
    failedSwitchRestores
  ],
  [
    '10. a switch or branch answer with nothing pending is ignored',
    unexpectedAnswerIsIgnored
  ]
];

describe(`navigation properties (${PROFILE}, ${NUM_RUNS} runs each)`, () => {
  for (const [name, check] of PROPERTIES) {
    it(name, () => {
      fc.assert(runs(check), fcParams());
    });
  }

  it('the navigator is deterministic: the same input gives the same output', () => {
    fc.assert(
      runs(() => undefined, navigator, true),
      fcParams()
    );
  });
});

describe('mutation check: the properties catch deliberately broken navigators', () => {
  it('a navigator that shows every answer as it arrives is caught by property 4', () => {
    const showsEveryAnswer: Navigate = (state, input) => {
      const r = navigator(state, input);
      const v = state.view;
      if (
        input.type !== 'stateReceived' ||
        !r.refusal ||
        v.tick === undefined
      ) {
        return r;
      }
      return {
        state: {
          ...state,
          view: {
            ...v,
            shown: {
              requestNo: input.requestNo,
              branchId: v.branchId,
              tick: v.tick,
              result: input.result
            },
            freshness: 'current'
          }
        },
        requests: []
      };
    };
    const outcome = fc.check(
      runs(noSupersededAnswer, showsEveryAnswer),
      fcParams()
    );
    expect(outcome.failed).toBe(true);
    // The real navigator passes the same property on the same sequences.
    expect(
      fc.check(runs(noSupersededAnswer), { ...fcParams(), seed: outcome.seed })
        .failed
    ).toBe(false);
  });

  it('a navigator that branches inside a run is caught by property 6', () => {
    const ignoresRuns: Navigate = (state, input) => {
      const r = navigator(state, input);
      if (
        input.type !== 'continueFromHere' ||
        r.refusal?.kind !== 'run_in_progress'
      ) {
        return r;
      }
      const { view } = state;
      return {
        state: {
          ...state,
          pending: {
            kind: 'create',
            fromBranchId: view.branchId,
            fromTick: view.tick as number
          }
        },
        requests: [
          {
            type: 'createBranch',
            fromBranchId: view.branchId,
            fromTick: view.tick as number
          }
        ]
      };
    };
    expect(
      fc.check(runs(noUnsettledBranching, ignoresRuns), fcParams()).failed
    ).toBe(true);
  });
});

describe('mutation check: more broken navigators, each caught by the property that guards the rule', () => {
  const lookingBack = (s: INavState): boolean => s.view.mode === 'lookingBack';

  const MUTANTS: Array<[string, string, Navigate]> = [
    [
      'back to now shows the old answer as current',
      '4. a superseded',
      (state, input) => {
        const r = navigator(state, input);
        if (input.type !== 'backToNow' || r.refusal) {
          return r;
        }
        return {
          ...r,
          state: {
            ...r.state,
            view: { ...r.state.view, freshness: 'current' }
          }
        };
      }
    ],
    [
      'a notification moves the view while looking back',
      '7. looking back',
      (state, input) => {
        const r = navigator(state, input);
        const tip = r.state.active.tip;
        if (
          input.type !== 'historyAdvanced' ||
          !lookingBack(state) ||
          !lookingBack(r.state) ||
          tip === undefined
        ) {
          return r;
        }
        return {
          ...r,
          state: { ...r.state, view: { ...r.state.view, tick: tip } }
        };
      }
    ],
    [
      'a jump to another branch changes the active branch',
      '3. only switched',
      (state, input) => {
        const r = navigator(state, input);
        if (input.type !== 'jumpTo' || r.refusal) {
          return r;
        }
        return {
          ...r,
          state: {
            ...r.state,
            active: { ...r.state.active, branchId: input.branchId }
          }
        };
      }
    ],
    [
      'the count goes up on a duplicate notification',
      '7. looking back',
      (state, input) => {
        const r = navigator(state, input);
        if (
          input.type !== 'historyAdvanced' ||
          !lookingBack(r.state) ||
          r.state.newSinceLookingBack !== state.newSinceLookingBack
        ) {
          return r;
        }
        return {
          ...r,
          state: {
            ...r.state,
            newSinceLookingBack: r.state.newSinceLookingBack + 1
          }
        };
      }
    ],
    [
      'a failed switch keeps the change pending',
      '9. a failed switch',
      (state, input) => {
        const r = navigator(state, input);
        if (input.type !== 'switchFailed' || !state.pending) {
          return r;
        }
        return { ...r, state: { ...r.state, pending: state.pending } };
      }
    ],
    [
      'a duplicate switched answer changes the state',
      '10. a switch or branch answer',
      (state, input) => {
        const r = navigator(state, input);
        if (input.type !== 'switched' || state.pending || !r.refusal) {
          return r;
        }
        return {
          state: { ...state, lastRequestNo: state.lastRequestNo + 1 },
          requests: []
        };
      }
    ]
  ];

  for (const [name, property, mutant] of MUTANTS) {
    it(`${name}: caught by property ${property.split('.')[0]}`, () => {
      const entry = PROPERTIES.find(([n]) => n.startsWith(property));
      expect(entry).toBeDefined();
      const outcome = fc.check(
        runs((entry as [string, StepCheck])[1], mutant),
        fcParams()
      );
      expect(outcome.failed).toBe(true);
    });
  }
});
