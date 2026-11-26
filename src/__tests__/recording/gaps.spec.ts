import type { RecorderEvent } from 'blaym-jupyter-types';
import { GapKeeper, Outbox } from '../../recording';
import type { EventDraft } from '../../recording';
import { FakeTime, FakeTransport, settle } from './generate';

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

describe('browser gaps (Task 20)', () => {
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

  test('a gap left open by a killed instance is closed once by the next, whichever route comes first', () => {
    const { keeper, out } = rig();
    keeper.close({ gapId: 'old' }); // from the server's openGaps
    keeper.close({ gapId: 'old' });
    expect(out.map(e => e.draft.payload)).toEqual([{ gapId: 'old' }]);
  });

  test('the outbox puts a gap ahead of a held snapshot but behind what is in flight', async () => {
    const time = new FakeTime();
    const transport = new FakeTransport();
    const outbox = new Outbox({
      transport,
      scheduler: time,
      clock: time,
      explorationId: 'nb',
      editor: 'jupyterlab',
      instanceId: 't',
      snapshotter: async () =>
        ({
          eventId: 's',
          type: 'notebook_snapshot'
        }) as unknown as RecorderEvent
    });
    const e = (id: string, type = 'cell_source_changed') =>
      ({ eventId: id, type, payload: {} }) as unknown as RecorderEvent;
    outbox.enqueue(e('a'));
    outbox.enqueue(e('snap', 'notebook_snapshot'));
    outbox.enqueue(e('gap', 'recording_gap_started'), true);
    await settle();
    const sent = ([] as string[]).concat(
      ...transport.requests.map((r: any) => r.events.map((x: any) => x.eventId))
    );
    expect(sent.slice(0, 3)).toEqual(['a', 'gap', 'snap']);
  });
});
