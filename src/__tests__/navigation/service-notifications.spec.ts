import type { HistoryAdvanced, HistoryOutline } from 'blaym-jupyter-types';
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

describe('service notifications through the navigator', () => {
  it('in order, and each notice twice, the outline equals the one built from the events', () => {
    expect(files.length).toBeGreaterThanOrEqual(6);
    for (const file of files) {
      const f = load(file);
      const target = normal(f.outline);
      const r = new Run(f);
      f.notices.forEach(n => {
        r.notify(n);
        const after = r.state;
        r.notify(n);
        expect(r.state).toEqual(after);
      });
      expect(r.refusals).toEqual([]);
      // following now, the navigator never asks for missed events
      expect(fetches(r)).toEqual([]);
      expect(r.outline).toEqual(target);
    }
  });

  it('a notice swapped or dropped: a fetch is asked and the outline answer converges', () => {
    for (const file of files) {
      const f = load(file);
      const target = normal(f.outline);
      const real = f.notices.filter(n => n.tick > f.initialOutline.highestTick);
      for (let i = 0; i < real.length - 1; i++) {
        for (const swap of [true, false]) {
          const r = new Run(f);
          real.slice(0, i).forEach(n => r.notify(n));
          const at = (r.state.outline as HistoryOutline).highestTick;
          if (swap) {
            r.notify(real[i + 1]);
            r.notify(real[i]);
            real.slice(i + 2).forEach(n => r.notify(n));
          } else {
            real.slice(i + 1).forEach(n => r.notify(n));
          }
          expect(fetches(r)[0]).toEqual({ type: 'fetchMissedSince', tick: at });
          r.send({ type: 'outlineReceived', outline: f.outline });
          expect(r.outline).toEqual(target);
        }
      }
    }
  });
});
