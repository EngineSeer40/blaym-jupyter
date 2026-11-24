import type { HistoryAdvanced, HistoryOutline } from 'blaym-jupyter-types';
import { validateContract } from 'blaym-jupyter-types';
import {
  initialState,
  navigator,
  INavState,
  NavRequest
} from '../../navigation';

// Node's own modules, declared here because the project does not depend on @types/node.
declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');

/**
 * The notifications the Python service produced (fixtures/service, written by
 * blaym_jupyter/tests/service/test_notification_fixtures.py) fed to the real navigator:
 * in order, duplicated, swapped, with one dropped, and answered with the
 * EventsSinceResponse shape. Only the navigator's public API is used.
 */

const DIR: string = join(__dirname, '..', '..', '..', 'fixtures', 'service');
const files: string[] = readdirSync(DIR)
  .filter((f: string) => f.endsWith('.json'))
  .sort();

interface IFixture {
  name: string;
  events: unknown[];
  notices: HistoryAdvanced[];
  outline: HistoryOutline;
  initialOutline: HistoryOutline;
  eventsSince: {
    request: unknown;
    response: { events: unknown[]; outline: HistoryOutline };
  };
}

const load = (f: string): IFixture =>
  JSON.parse(readFileSync(join(DIR, f), 'utf8'));

function normal(o: HistoryOutline): HistoryOutline {
  return {
    ...o,
    branches: [...o.branches].sort((a, b) =>
      a.branchId.localeCompare(b.branchId)
    )
  };
}

class Run {
  state: INavState;
  requests: NavRequest[] = [];
  refusals: string[] = [];
  constructor(f: IFixture) {
    this.state = initialState(f.initialOutline.rootBranchId);
    this.send({ type: 'outlineReceived', outline: f.initialOutline });
    this.requests = [];
  }
  send(input: Parameters<typeof navigator>[1]): void {
    const r = navigator(this.state, input);
    this.state = r.state;
    this.requests.push(...r.requests);
    if (r.refusal) {
      this.refusals.push(r.refusal.kind);
    }
  }
  notify(n: HistoryAdvanced): void {
    this.send({ type: 'historyAdvanced', notification: n });
  }
  get outline(): HistoryOutline {
    return normal(this.state.outline as HistoryOutline);
  }
}

const fetches = (r: Run) =>
  r.requests.filter(q => q.type === 'fetchMissedSince');

describe('service notifications through the S3b navigator', () => {
  it('finds the fixtures', () => {
    expect(files.length).toBeGreaterThanOrEqual(6);
  });

  describe.each(files.map(f => [f]))('%s', (file: string) => {
    const f = load(file);
    const target = normal(f.outline);

    it('every value validates against its contract', () => {
      f.events.forEach(e =>
        expect(validateContract('BlaymEvent', e).ok).toBe(true)
      );
      f.notices.forEach(n =>
        expect(validateContract('HistoryAdvanced', n).ok).toBe(true)
      );
      expect(validateContract('HistoryOutline', f.outline).ok).toBe(true);
      expect(validateContract('HistoryOutline', f.initialOutline).ok).toBe(
        true
      );
      expect(
        validateContract('EventsSinceRequest', f.eventsSince.request).ok
      ).toBe(true);
      expect(
        validateContract('EventsSinceResponse', f.eventsSince.response).ok
      ).toBe(true);
    });

    it('in order: the outline equals the one built from the events', () => {
      const r = new Run(f);
      f.notices.forEach(n => r.notify(n));
      expect(r.refusals).toEqual([]);
      // following now, the navigator asks for the new state; it never asks for missed events
      expect(fetches(r)).toEqual([]);
      expect(r.outline).toEqual(target);
    });

    it('every notice twice changes nothing after the first', () => {
      const r = new Run(f);
      f.notices.forEach(n => {
        r.notify(n);
        const after = r.state;
        r.notify(n);
        expect(r.state).toEqual(after);
      });
      expect(r.refusals).toEqual([]);
      expect(r.outline).toEqual(target);
    });

    it('two adjacent notices swapped: the later one waits, a fetch is asked, the outline answer converges', () => {
      const real = f.notices.filter(n => n.tick > f.initialOutline.highestTick);
      for (let i = 0; i + 1 < real.length; i++) {
        const r = new Run(f);
        real.slice(0, i).forEach(n => r.notify(n));
        const before = r.state.outline as HistoryOutline;
        r.notify(real[i + 1]);
        expect(r.state.outline).toEqual(before);
        expect(fetches(r)).toEqual([
          { type: 'fetchMissedSince', tick: before.highestTick }
        ]);
        r.notify(real[i]);
        real.slice(i + 2).forEach(n => r.notify(n));
        // the late notice applied, the early duplicate is ignored: still converges after the answer
        r.send({ type: 'outlineReceived', outline: f.outline });
        expect(r.outline).toEqual(target);
      }
    });

    it('one notice dropped: a fetch is asked and the outline answer converges', () => {
      const real = f.notices.filter(n => n.tick > f.initialOutline.highestTick);
      for (let i = 0; i < real.length - 1; i++) {
        const r = new Run(f);
        real.slice(0, i).forEach(n => r.notify(n));
        const at = (r.state.outline as HistoryOutline).highestTick;
        real.slice(i + 1).forEach(n => r.notify(n));
        expect(fetches(r).length).toBeGreaterThan(0);
        expect(fetches(r)[0]).toEqual({ type: 'fetchMissedSince', tick: at });
        r.send({ type: 'outlineReceived', outline: f.outline });
        expect(r.outline).toEqual(target);
      }
    });

    it('the eventsSince answer: its outline converges from the start', () => {
      const r = new Run(f);
      r.send({
        type: 'outlineReceived',
        outline: f.eventsSince.response.outline
      });
      expect(r.refusals).toEqual([]);
      expect(r.outline).toEqual(target);
    });
  });
});
