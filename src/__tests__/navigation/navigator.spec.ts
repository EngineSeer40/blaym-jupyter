import type { RebuildResult } from 'blaym-jupyter-types';
import { initialState, navigator, NavInput } from '../../navigation';
import {
  branchEntry,
  deepFreeze,
  okAt,
  outlineOf,
  range,
  Scenario
} from './scenario';

const MAIN = { id: 'main', ticks: range(1, 10) };
const B1 = { id: 'b1', from: ['main', 5] as [string, number], ticks: [11, 12] };
const B2 = { id: 'b2', from: ['main', 7] as [string, number], ticks: [13, 14] };

describe('navigator rules, checked against the code', () => {
  it('looking around never changes the active branch', () => {
    const s = new Scenario(outlineOf(MAIN, B1, B2));
    const inputs: NavInput[] = [
      { type: 'jumpTo', branchId: 'b2', tick: 13 },
      { type: 'stepBack' },
      { type: 'stepForward' },
      { type: 'selectBranch', branchId: 'b1' },
      { type: 'jumpToStart' },
      { type: 'jumpTo', branchId: 'nope', tick: 1 },
      { type: 'jumpTo', branchId: 'b1', tick: 0 },
      { type: 'stepBack' },
      { type: 'backToNow' }
    ];
    for (const i of inputs) {
      s.send(i);
      expect(s.active).toBe('main');
    }
  });

  it('refusals return the very same state and no requests', () => {
    const s = new Scenario(outlineOf(MAIN));
    const before = s.state;
    for (const i of [
      { type: 'stepForward' },
      { type: 'jumpTo', branchId: 'zzz', tick: 1 },
      { type: 'switchTo', branchId: 'main' },
      { type: 'switched', branchId: 'main' },
      { type: 'switchFailed', reason: 'x' },
      { type: 'branchCreated', branch: branchEntry({ id: 'q', ticks: [] }) }
    ] as NavInput[]) {
      s.send(i);
      expect(s.refusal).toBeDefined();
      expect(s.requests).toEqual([]);
      expect(s.state).toBe(before);
    }
  });

  it('an unsound outline is refused and the old one is kept', () => {
    const s = new Scenario(outlineOf(MAIN));
    const bad = outlineOf(MAIN);
    bad.branches[0].ownTicks = [2, 1];
    bad.highestTick = 20;
    const before = s.state;
    s.send({ type: 'outlineReceived', outline: bad });
    expect(s.refusal?.kind).toBe('outline_unsound');
    expect(s.state).toBe(before);
  });

  it('an ok answer for a different position is never shown', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 4 });
    const f = s.lastFetch();
    for (const wrong of [okAt('main', 5), okAt('other', 4)]) {
      s.send({ type: 'stateReceived', requestNo: f.requestNo, result: wrong });
      expect(s.refusal?.kind).toBe('answer_mismatch');
      expect(s.shownAt).toBe('main@10');
    }
    s.answer();
    expect(s.shownAt).toBe('main@4');
  });

  it('an answer to an unknown request number is dropped, and an answer cannot be shown twice', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 4 });
    s.send({ type: 'stateReceived', requestNo: 999, result: okAt('main', 4) });
    expect(s.refusal?.kind).toBe('stale_answer');
    s.answer();
    s.send({ type: 'jumpTo', branchId: 'main', tick: 4 }); // reused, no fetch
    const n = s.allRequests.length;
    s.send({
      type: 'stateReceived',
      requestNo: s.lastFetch().requestNo,
      result: okAt('main', 4)
    });
    expect(s.refusal?.kind).toBe('stale_answer');
    expect(s.allRequests.length).toBe(n);
  });

  it('back to now never reports a state below the tip as current', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 3 }).answer();
    s.send({ type: 'backToNow' });
    expect(s.state.view.freshness).not.toBe('current');
    const f = s.lastFetch();
    s.advance('main', 11); // tip moves before the answer
    s.send({
      type: 'stateReceived',
      requestNo: f.requestNo,
      result: okAt('main', 10)
    });
    expect(s.refusal?.kind).toBe('stale_answer');
    expect(s.state.view.freshness).not.toBe('current');
    s.answer();
    expect(s.shownAt).toBe('main@11');
    expect(s.state.view.freshness).toBe('current');
  });

  it('backToNow at a reused tip answer is current only when the tip matches', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 3 }).answer();
    s.advance('main', 11);
    s.send({ type: 'backToNow' });
    expect(s.shownAt).toBe('main@3');
    expect(s.state.view.freshness).not.toBe('current');
  });

  it('while looking back, notifications of every kind never move the view', () => {
    const s = new Scenario(outlineOf(MAIN, B1));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 4 }).answer();
    const view = s.state.view;
    s.advance('main', 20); // gap: not applied
    expect(s.state.view).toEqual(view);
    s.advance('b1', 13); // other branch than active
    s.advance('main', 14, { runStarted: 'r' }); // out of order from 13? 14 follows 13
    s.advance('b1', 13); // duplicate
    s.advance('new', 15, {
      newBranch: {
        branchId: 'new',
        name: 'new',
        parentBranchId: 'b1',
        parentTick: 12,
        ownTicks: [],
        runSpans: []
      }
    });
    expect(s.state.view).toEqual(view);
    expect(s.at).toBe('main@4');
    // Only ticks on the active (main) line count: 14.
    expect(s.state.newSinceLookingBack).toBe(1);
  });

  it('continue and switch are refused while disconnected, with no request', () => {
    const s = new Scenario(outlineOf(MAIN, B1));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 4 });
    s.send({ type: 'disconnected' });
    s.send({ type: 'continueFromHere' });
    expect(s.refusal?.kind).toBe('disconnected');
    expect(s.requests).toEqual([]);
    s.send({ type: 'switchTo', branchId: 'b1' });
    expect(s.refusal?.kind).toBe('disconnected');
    expect(s.state.pending).toBeUndefined();
  });

  it('switching is refused when the active tip is inside an open run', () => {
    const s = new Scenario(outlineOf({ ...MAIN, runs: [[8]] }, B1));
    s.send({ type: 'switchTo', branchId: 'b1' });
    expect(s.refusal?.kind).toBe('run_in_progress');
    expect(s.requests).toEqual([]);
  });

  it('switchFailed differs from the state before the request only in pending', () => {
    const s = new Scenario(outlineOf(MAIN, B1));
    s.send({ type: 'jumpTo', branchId: 'b1', tick: 12 }).answer();
    const before = s.state;
    s.send({ type: 'switchTo', branchId: 'b1' });
    expect(s.state.pending).toEqual({ kind: 'switch', branchId: 'b1' });
    expect({ ...s.state, pending: undefined }).toEqual({
      ...before,
      pending: undefined
    });
    s.send({ type: 'switchFailed', reason: 'no' });
    expect(s.state).toEqual(before);
    // Same for a failed create (request numbers do not move).
    s.send({ type: 'continueFromHere' });
    s.send({ type: 'switchFailed', reason: 'no' });
    expect(s.state).toEqual(before);
  });

  it('a pending request survives looking around and notifications', () => {
    const s = new Scenario(outlineOf(MAIN, B1));
    s.send({ type: 'switchTo', branchId: 'b1' });
    s.send({ type: 'jumpTo', branchId: 'main', tick: 2 });
    s.advance('main', 11 + 2);
    expect(s.state.pending).toEqual({ kind: 'switch', branchId: 'b1' });
    expect(s.active).toBe('main');
  });

  it('stepping back past a child branch point stays on the child, forward re-enters its ticks', () => {
    const s = new Scenario(outlineOf(MAIN, B1), 'b1');
    s.send({ type: 'jumpTo', branchId: 'b1', tick: 12 });
    const seen: string[] = [s.at];
    for (let i = 0; i < 9; i++) {
      s.send({ type: 'stepBack' });
      seen.push(s.at);
    }
    expect(seen).toEqual(
      ['b1@12', 'b1@11', 'b1@5', 'b1@4', 'b1@3', 'b1@2', 'b1@1'].concat([
        'b1@1',
        'b1@1',
        'b1@1'
      ])
    );
    expect(s.refusal?.kind).toBe('at_start');
    for (let i = 0; i < 5; i++) {
      s.send({ type: 'stepForward' });
    }
    expect(s.at).toBe('b1@11');
    expect(s.state.view.branchId).toBe('b1');
  });

  it('decision B: a non-ok answer is held at its position', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 6 });
    const bad = {
      status: 'unknown_branch',
      branchId: 'main'
    } as unknown as RebuildResult;
    s.send({
      type: 'stateReceived',
      requestNo: s.lastFetch().requestNo,
      result: bad
    });
    expect(s.state.view.shown).toMatchObject({
      branchId: 'main',
      tick: 6,
      result: bad
    });
  });

  it('the navigator is deterministic and the initial state is not shared', () => {
    const a = deepFreeze(initialState('main'));
    const input: NavInput = {
      type: 'outlineReceived',
      outline: outlineOf(MAIN)
    };
    expect(navigator(a, input)).toEqual(navigator(a, input));
  });

  it('reconnect re-asks an in-flight answer under a new number', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 4 });
    const first = s.lastFetch().requestNo;
    s.send({ type: 'disconnected' }).send({ type: 'reconnected' });
    expect(s.lastFetch().requestNo).toBeGreaterThan(first);
    s.send({
      type: 'stateReceived',
      requestNo: first,
      result: okAt('main', 4)
    });
    expect(s.refusal?.kind).toBe('stale_answer');
  });
});

describe('run spans carried by notifications', () => {
  const child = (runIds: string[]) => ({
    branchId: 'c',
    name: 'c',
    parentBranchId: 'main',
    parentTick: 4,
    ownTicks: [],
    runSpans: runIds.map(r => ({ runId: r, startedTick: 3 }))
  });
  const spans = (s: Scenario, id: string) =>
    s.state.outline?.branches.find(b => b.branchId === id)?.runSpans;

  it('the parent finishing after the branch point leaves the child span open; the child closes only its own', () => {
    const s = new Scenario(outlineOf({ id: 'main', ticks: [1, 2] }));
    s.advance('main', 3, { runStarted: 'r' });
    s.advance('main', 4);
    s.advance('c', 5, { newBranch: child(['r']) });
    s.advance('main', 6, { runsEnded: ['r'] });
    expect(spans(s, 'main')).toEqual([
      { runId: 'r', startedTick: 3, endedTick: 6 }
    ]);
    expect(spans(s, 'c')).toEqual([{ runId: 'r', startedTick: 3 }]);
    // The child is still unsettled at its tip, so switching to it is fine only
    // from a settled active tip, but continuing from the child is refused.
    s.send({ type: 'selectBranch', branchId: 'c' });
    s.send({ type: 'continueFromHere' });
    expect(s.refusal?.kind).toBe('run_in_progress');
    s.advance('c', 7, { runsEnded: ['r'] });
    expect(spans(s, 'c')).toEqual([
      { runId: 'r', startedTick: 3, endedTick: 7 }
    ]);
    expect(spans(s, 'main')).toEqual([
      { runId: 'r', startedTick: 3, endedTick: 6 }
    ]);
    s.send({ type: 'selectBranch', branchId: 'c' });
    s.send({ type: 'continueFromHere' });
    expect(s.refusal).toBeUndefined();
  });

  it('a kernel restart closes several spans at once, on its own line only', () => {
    const s = new Scenario(outlineOf({ id: 'main', ticks: [1] }));
    s.advance('main', 2, { runStarted: 'a' });
    s.advance('main', 3, { runStarted: 'b' });
    s.advance('main', 4, { runsEnded: ['a', 'b', 'not-here'] });
    expect(spans(s, 'main')).toEqual([
      { runId: 'a', startedTick: 2, endedTick: 4 },
      { runId: 'b', startedTick: 3, endedTick: 4 }
    ]);
  });

  it('a repeated run start or end does not change the spans', () => {
    const s = new Scenario(outlineOf({ id: 'main', ticks: [1] }));
    s.advance('main', 2, { runStarted: 'a' });
    s.advance('main', 2, { runStarted: 'a' });
    s.advance('main', 3, { runsEnded: ['a'] });
    s.advance('main', 3, { runsEnded: ['a'] });
    expect(spans(s, 'main')).toEqual([
      { runId: 'a', startedTick: 2, endedTick: 3 }
    ]);
  });
});
