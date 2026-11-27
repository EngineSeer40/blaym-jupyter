import type { RecorderEvent } from 'blaym-notebook-types';
import { EventBuilder, RunTracker } from '../../recording';
import type { EventDraft } from '../../recording';
import { FakeTime, FakeTransport, POLICY, SeqIds, settle } from './generate';

/** A tracker on the real builder, with a session that only queues and counts revisions. */
function rig() {
  const time = new FakeTime();
  const transport = new FakeTransport();
  const ids = new SeqIds();
  const builder = new EventBuilder({
    clock: time,
    ids,
    transport,
    explorationId: 'nb1',
    editor: 'vscode',
    originId: 'o1',
    policy: POLICY
  });
  builder.noteStatus({ c1: 1, c2: 1 });
  const events: RecorderEvent[] = [];
  let pending: Array<Promise<unknown>> = [];
  const queue = (d: EventDraft) => {
    pending.push(builder.build(d).then(e => void events.push(e)));
  };
  const tracker = new RunTracker({
    session: {
      queue,
      beforeRun: () => undefined,
      cellRevision: async id => {
        await Promise.all(pending);
        pending = [];
        const revision = builder.revisionOf(id);
        return revision === undefined ? undefined : { cellId: id, revision };
      },
      outputsCleared: id =>
        queue({
          type: 'cell_outputs_cleared',
          payload: { cellId: id },
          cellId: id
        })
    },
    transport,
    explorationId: () => 'nb1',
    clock: time,
    scheduler: time,
    ids
  });
  const flush = async () => {
    time.advance(250);
    await settle(50);
    await Promise.all(pending);
  };
  const kinds = () => events.map(e => e.type);
  const out = (text: string) => ({ output_type: 'stream', text });
  return { time, transport, tracker, events, queue, flush, kinds, out, settle };
}

describe('RunTracker', () => {
  test('a run starts at a new execution order and finishes on success; a bare {} starts nothing', async () => {
    const { tracker, transport, events, kinds, flush } = rig();
    tracker.observeCell({ cellId: 'c1', outputs: [] });
    await flush();
    expect(events).toHaveLength(0);
    tracker.observeCell({ cellId: 'c1', executionOrder: 1, outputs: [] });
    tracker.observeCell({
      cellId: 'c1',
      executionOrder: 1,
      success: true,
      outputs: []
    });
    tracker.observeCell({
      cellId: 'c1',
      executionOrder: 1,
      success: false,
      outputs: []
    });
    tracker.observeCell({ cellId: 'c2', executionOrder: 2, outputs: [] });
    tracker.observeCell({
      cellId: 'c2',
      executionOrder: 2,
      success: false,
      errorName: 'KeyboardInterrupt',
      outputs: []
    });
    await flush();
    expect(kinds()).toEqual([
      'run_started',
      'run_finished',
      'run_started',
      'run_finished'
    ]);
    expect(events[0].payload).toMatchObject({
      cellId: 'c1',
      cellRevision: 1,
      runCount: 1,
      codeLink: 'by_time'
    });
    expect(events[0].links).toEqual({
      cellId: 'c1',
      runId: (events[0].payload as { runId: string }).runId,
      cellRevision: 1
    });
    expect(events[1].links).toEqual({
      cellId: 'c1',
      runId: (events[0].payload as { runId: string }).runId
    });
    expect(events[1].payload).toMatchObject({ status: 'ok' });
    expect(events[3].payload).toMatchObject({ status: 'interrupted' });
    // a failing output conversion still ends the run
    transport.prepareOutputs = () => Promise.reject(new Error('down'));
    tracker.observeCell({ cellId: 'c2', executionOrder: 3, outputs: [] });
    tracker.observeCell({
      cellId: 'c2',
      executionOrder: 3,
      success: true,
      outputs: [{ output_type: 'stream', text: 'x' }]
    });
    await flush();
    expect(kinds().slice(4)).toEqual(['run_started', 'run_finished']);
  });

  test('outputs are added, replaced when an item changes, and cleared when the list shrinks', async () => {
    const { tracker, events, kinds, flush, out } = rig();
    const obs = (outputs: Record<string, unknown>[], success?: boolean) =>
      tracker.observeCell({
        cellId: 'c1',
        executionOrder: 1,
        success,
        outputs
      });
    obs([]);
    obs([out('a')]);
    obs([out('ab'), out('x')]);
    await flush();
    expect(kinds()).toEqual(['run_started', 'output_added', 'output_added']);
    obs([out('abc'), out('x')]);
    await flush();
    expect(kinds().slice(3)).toEqual(['output_replaced']);
    expect(events[3].payload).toMatchObject({ outputIndex: 0 });
    obs([out('new')]);
    obs([out('new')], true);
    await flush();
    expect(kinds().slice(4)).toEqual([
      'outputs_cleared',
      'output_added',
      'run_finished'
    ]);
    expect(events[4].payload).toMatchObject({ wait: false });
  });

  test('an empty list outside a run clears the cell outputs', async () => {
    const { tracker, kinds, flush, out } = rig();
    tracker.observeCell({
      cellId: 'c1',
      executionOrder: 1,
      success: true,
      outputs: [out('a')]
    });
    tracker.observeCell({
      cellId: 'c1',
      executionOrder: 1,
      success: true,
      outputs: []
    });
    await flush();
    expect(kinds()).toEqual(['cell_outputs_cleared']);
  });

  test('a restart or crash leaves open runs unfinished and each reason is right', async () => {
    const { tracker, events, kinds, flush } = rig();
    const started = {
      state: 'started',
      kernelName: 'python3',
      language: 'python'
    } as const;
    tracker.observeKernel(started);
    tracker.observeKernel(started);
    tracker.observeCell({ cellId: 'c1', executionOrder: 1, outputs: [] });
    tracker.observeKernel({ state: 'restarting' });
    tracker.observeKernel(started);
    tracker.observeKernel(started);
    tracker.observeCell({ cellId: 'c1', executionOrder: 1, outputs: [] });
    tracker.observeKernel({ state: 'dead' });
    // VS Code's crash text on the last cell is not a run and is not stored
    tracker.observeCell({
      cellId: 'c1',
      executionOrder: 2,
      outputs: [{ output_type: 'error', ename: 'Crash', evalue: 'crashed' }]
    });
    await flush();
    expect(kinds()).toHaveLength(5);
    tracker.observeKernel(started);
    await flush();
    expect(kinds()).toEqual([
      'kernel_started',
      'run_started',
      'kernel_restarted',
      'run_started',
      'kernel_restarted',
      'kernel_started'
    ]);
    // after the new kernel a real run of the same cell is recorded normally
    tracker.observeCell({ cellId: 'c1', executionOrder: 1, outputs: [] });
    tracker.observeCell({
      cellId: 'c1',
      executionOrder: 1,
      success: true,
      outputs: [{ output_type: 'stream', text: 'ok' }]
    });
    await flush();
    expect(kinds().slice(6)).toEqual([
      'run_started',
      'output_added',
      'run_finished'
    ]);
    expect(events[2].payload).toMatchObject({ reason: 'requested' });
    expect(events[4].payload).toMatchObject({ reason: 'crash' });
    const ids = [0, 2, 4, 5].map(
      i => (events[i].payload as { kernelId: string }).kernelId
    );
    expect(events[2].payload).toMatchObject({ kernelId: ids[0] });
    expect(events[4].payload).toMatchObject({
      kernelId: expect.not.stringMatching(ids[0])
    });
    expect(events[5].payload).not.toMatchObject({ kernelId: ids[0] });
  });

  test('an edit made during a run is a later revision and the run keeps its own', async () => {
    const { tracker, events, queue, flush } = rig();
    tracker.observeCell({ cellId: 'c1', executionOrder: 1, outputs: [] });
    await flush();
    queue({
      type: 'cell_source_changed',
      payload: { cellId: 'c1', code: 'x = 2' },
      cellId: 'c1'
    });
    tracker.observeCell({
      cellId: 'c1',
      executionOrder: 1,
      success: true,
      outputs: []
    });
    await flush();
    expect(events[0].payload).toMatchObject({ cellRevision: 1 });
    expect(events[1].payload).toMatchObject({ revision: 2 });
    expect(events[2].type).toBe('run_finished');
  });
});
