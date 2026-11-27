import { branchEntry, okAt, outlineOf, range, Scenario } from './scenario';

const MAIN = { id: 'main', ticks: range(1, 10) };
const B1 = { id: 'b1', from: ['main', 5] as [string, number], ticks: [11, 12] };
const B2 = { id: 'b2', from: ['main', 7] as [string, number], ticks: [13, 14] };

describe('named navigation scenarios', () => {
  it('1. create branch, switch, jump around, return: the tip comes first', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 5 }).answer();
    s.send({ type: 'continueFromHere' });
    expect(s.requests).toEqual([
      { type: 'createBranch', fromBranchId: 'main', fromTick: 5 }
    ]);
    s.send({
      type: 'branchCreated',
      branch: branchEntry({ ...B1, ticks: [11] })
    }).answer();
    expect(s.active).toBe('b1');
    expect(s.state.view.mode).toBe('following');

    s.send({ type: 'jumpTo', branchId: 'b1', tick: 3 }).answer();
    s.send({ type: 'jumpTo', branchId: 'main', tick: 9 }).answer();
    s.send({ type: 'stepBack' }).answer();
    expect(s.state.view.mode).toBe('lookingBack');

    s.send({ type: 'backToNow' });
    // Before the answer, the old content is marked as not current.
    expect(s.at).toBe('b1@11');
    expect(s.state.view.freshness).toBe('loading');
    expect(s.shownAt).not.toBe('b1@11');
    expect(s.lastFetch()).toMatchObject({ branchId: 'b1', tick: 11 });
    s.answer();
    expect(s.shownAt).toBe('b1@11');
    expect(s.state.view.freshness).toBe('current');
    expect(s.state.view.mode).toBe('following');
  });

  it('5. continue from here is refused while a run is open, allowed after it finished', () => {
    const s = new Scenario(outlineOf({ ...MAIN, runs: [[3, 7]] }));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 5 });
    s.send({ type: 'continueFromHere' });
    expect(s.refusal?.kind).toBe('run_in_progress');
    expect(s.requests).toEqual([]);
    expect(s.state.pending).toBeUndefined();

    s.send({ type: 'jumpTo', branchId: 'main', tick: 7 });
    s.send({ type: 'continueFromHere' });
    expect(s.refusal).toBeUndefined();
    expect(s.requests).toEqual([
      { type: 'createBranch', fromBranchId: 'main', fromTick: 7 }
    ]);
  });

  it('7. an answer to an older request is dropped', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 3 });
    const first = s.lastFetch();
    s.send({ type: 'jumpTo', branchId: 'main', tick: 6 });
    const second = s.lastFetch();
    s.send({
      type: 'stateReceived',
      requestNo: second.requestNo,
      result: okAt('main', 6)
    });
    expect(s.shownAt).toBe('main@6');
    s.send({
      type: 'stateReceived',
      requestNo: first.requestNo,
      result: okAt('main', 3)
    });
    expect(s.refusal?.kind).toBe('stale_answer');
    expect(s.shownAt).toBe('main@6');

    // Also when the old answer comes first.
    s.send({ type: 'jumpTo', branchId: 'main', tick: 2 });
    const third = s.lastFetch();
    s.send({ type: 'jumpTo', branchId: 'main', tick: 9 });
    s.send({
      type: 'stateReceived',
      requestNo: third.requestNo,
      result: okAt('main', 2)
    });
    expect(s.shownAt).toBe('main@6');
    expect(s.state.view.freshness).toBe('loading');
  });

  it('9. a failed switch leaves everything as it was', () => {
    const s = new Scenario(outlineOf(MAIN, B1));
    s.send({ type: 'jumpTo', branchId: 'b1', tick: 11 }).answer();
    // Main gained a step; the outline is read again so run spans are exact.
    s.send({
      type: 'outlineReceived',
      outline: outlineOf({ ...MAIN, ticks: [...MAIN.ticks, 13] }, B1)
    });
    const before = s.state;
    s.send({ type: 'switchTo', branchId: 'b1' });
    expect(s.requests).toEqual([{ type: 'switchBranch', branchId: 'b1' }]);
    s.send({ type: 'switchFailed', reason: 'the notebook is closed' });
    expect(s.refusal).toMatchObject({
      kind: 'switch_failed',
      reason: 'the notebook is closed'
    });
    expect(s.state).toEqual(before);
    expect(s.active).toBe('main');
  });

  it('11. disconnect while looking back: asks for what it missed, count right, view still', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 4 }).answer();
    s.advance('main', 11);
    s.send({ type: 'disconnected' });
    // Events 12 and 13 were stored while disconnected and never announced.
    s.send({ type: 'reconnected' });
    expect(s.requests).toEqual([{ type: 'fetchMissedSince', tick: 11 }]);
    s.advance('main', 12).advance('main', 13);
    expect(s.state.newSinceLookingBack).toBe(3);
    expect(s.at).toBe('main@4');

    // A notification that skips ahead is not applied; it asks again.
    s.advance('main', 15);
    expect(s.requests).toEqual([{ type: 'fetchMissedSince', tick: 13 }]);
    expect(s.state.newSinceLookingBack).toBe(3);
  });

  it('13. two children of one parent worked alternately: each shows its own line', () => {
    const s = new Scenario(outlineOf(MAIN, B1, B2), 'b1');
    s.send({ type: 'selectBranch', branchId: 'b2' });
    expect(s.at).toBe('b2@14');
    s.send({ type: 'stepBack' }).send({ type: 'stepBack' });
    expect(s.at).toBe('b2@7');
    s.send({ type: 'selectBranch', branchId: 'b1' });
    expect(s.at).toBe('b1@12');
    expect(s.state.view.mode).toBe('following');
    s.send({ type: 'stepBack' }).send({ type: 'stepBack' });
    expect(s.at).toBe('b1@5');

    s.send({ type: 'switchTo', branchId: 'b2' }).send({
      type: 'switched',
      branchId: 'b2'
    });
    expect(s.at).toBe('b2@14');
    s.advance('b1', 15);
    expect(s.state.newSinceLookingBack).toBe(0);
    expect(s.at).toBe('b2@14');
    s.advance('b2', 16);
    expect(s.at).toBe('b2@16');
    s.send({ type: 'selectBranch', branchId: 'b1' });
    expect(s.at).toBe('b1@15');
    s.send({ type: 'backToNow' });
    expect(s.at).toBe('b2@16');
    expect(s.active).toBe('b2');
  });

  it('4. edits arrive while looking back: counted, view still, back to now catches up', () => {
    const s = new Scenario(outlineOf(MAIN));
    s.send({ type: 'jumpTo', branchId: 'main', tick: 4 }).answer();
    s.advance('main', 11).advance('main', 12);
    expect(s.state.newSinceLookingBack).toBe(2);
    expect(s.at).toBe('main@4');
    expect(s.requests).toEqual([]);
    s.advance('main', 12); // the same notification again
    expect(s.state.newSinceLookingBack).toBe(2);

    s.send({ type: 'backToNow' });
    expect(s.at).toBe('main@12');
    expect(s.state.newSinceLookingBack).toBe(0);
    expect(s.lastFetch()).toMatchObject({ branchId: 'main', tick: 12 });
    expect(s.state.view.freshness).toBe('loading');
  });

  it('10. a second switch while one is pending is refused', () => {
    const s = new Scenario(outlineOf(MAIN, B1, B2));
    s.send({ type: 'switchTo', branchId: 'b1' });
    s.send({ type: 'switchTo', branchId: 'b2' });
    expect(s.refusal?.kind).toBe('change_pending');
    expect(s.requests).toEqual([]);
    s.send({ type: 'continueFromHere' });
    expect(s.refusal?.kind).toBe('change_pending');
    // The first one still completes.
    s.send({ type: 'switched', branchId: 'b1' });
    expect(s.active).toBe('b1');
    expect(s.state.pending).toBeUndefined();
  });
});
