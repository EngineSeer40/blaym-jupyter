import { GapKeeper } from '../../recording';
import type { EventDraft } from '../../recording';

function rig() {
  const out: Array<{ draft: EventDraft; first: boolean }> = [];
  let n = 0;
  const keeper = new GapKeeper({
    ids: { uuid: () => `gap${++n}` },
    emit: (draft, first) => out.push({ draft, first })
  });
  return { keeper, out };
}
const types = (o: ReturnType<typeof rig>['out']) => o.map(e => e.draft.type);

describe('browser gaps', () => {
  test('one open gap per reason, closed once by the snapshot', () => {
    const { keeper, out } = rig();
    keeper.open('unknown');
    keeper.open('unknown'); // a held snapshot retrying: still the same gap
    keeper.closeAll();
    keeper.closeAll();
    expect(types(out)).toEqual([
      'recording_gap_started',
      'recording_gap_ended'
    ]);
    expect(out[0].draft.payload).toEqual({
      gapId: 'gap1',
      source: 'browser',
      reason: 'unknown'
    });
    expect(out[0].first).toBe(true); // ahead of a held snapshot
  });
});
