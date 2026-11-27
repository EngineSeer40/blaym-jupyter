import { NavInput } from '../../navigation';
import { okAt, outlineOf, range, Scenario } from './scenario';

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

  it('switching is refused when the active tip is inside an open run', () => {
    const s = new Scenario(outlineOf({ ...MAIN, runs: [[8]] }, B1));
    s.send({ type: 'switchTo', branchId: 'b1' });
    expect(s.refusal?.kind).toBe('run_in_progress');
    expect(s.requests).toEqual([]);
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
});
