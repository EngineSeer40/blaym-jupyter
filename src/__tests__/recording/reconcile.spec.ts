import * as fc from 'fast-check';
import {
  Applier,
  appliedModel,
  applyOp,
  expectedModel,
  FakeNotebook,
  Harness,
  stepsArb,
  status
} from './generate';

jest.setTimeout(120000);

async function started(setup?: (nb: FakeNotebook) => void): Promise<Harness> {
  const nb = new FakeNotebook();
  setup?.(nb);
  const h = new Harness(nb);
  await h.snapshot();
  return h;
}

describe('reconcile and builder: properties over generated editor operations', () => {
  test('events applied by an independent applier equal the notebook after every step; ids unique; no delete+insert pairs; revisions never decrease', async () => {
    await fc.assert(
      fc.asyncProperty(stepsArb, async steps => {
        const h = await started(nb => {
          nb.insert(0, 'code', 'a');
          nb.insert(1, 'markdown', 'b');
        });
        const applier = new Applier();
        let applied = 0;
        const catchUp = () => {
          for (; applied < h.events.length; applied++) applier.apply(h.events[applied]);
        };
        catchUp();
        for (const step of steps) {
          step.forEach(o => applyOp(h.nb, o));
          const produced = await h.change();
          await h.finish();
          catchUp();
          // the step's own events: no id both deleted and inserted
          const deleted = new Set(produced.filter(e => e.type === 'cell_deleted').map(e => (e as any).payload.cellId));
          produced
            .filter(e => e.type === 'cell_inserted')
            .forEach(e => expect(deleted.has((e as any).payload.cellId)).toBe(false));
          expect(appliedModel(applier)).toEqual(expectedModel(h.nb, h.identity));
          const ids = applier.cells.map(c => c.id);
          expect(new Set(ids).size).toBe(ids.length);
        }
        expect(applier.problems).toEqual([]);
      }),
      { numRuns: 300 }
    );
  });

  test('a single move is exactly one cell_moved event (never a delete plus an insert)', async () => {
    await fc.assert(
      fc.asyncProperty(fc.integer({ min: 2, max: 9 }), fc.nat(20), fc.nat(20), async (n, from, to) => {
        const h = await started(nb => {
          for (let i = 0; i < n; i++) nb.insert(i, 'code', `c${i}`);
        });
        applyOp(h.nb, { op: 'move', from, to });
        const produced = await h.change();
        const same = from % n === to % n;
        expect(produced.map(e => e.type)).toEqual(same ? [] : ['cell_moved']);
      }),
      { numRuns: 200 }
    );
  });

  test('a type change is one cell_type_changed with the same id', async () => {
    const h = await started(nb => nb.insert(0, 'code', 'x'));
    h.nb.list[0].type = 'markdown';
    const produced = await h.change();
    expect(produced.map(e => e.type)).toEqual(['cell_type_changed']);
    expect((produced[0] as any).payload).toEqual({ cellId: h.nb.list[0].id, cellType: 'markdown' });
  });
});

describe('reconcile: whole-notebook changes become one resync', () => {
  test('all ids replaced (reload or revert) gives a resync and a fresh snapshot, not repair events', async () => {
    const h = await started(nb => {
      nb.insert(0, 'code', 'a');
      nb.insert(1, 'code', 'b');
    });
    h.nb.reload();
    const produced = await h.change();
    expect(h.resyncs).toBe(1);
    expect(produced.map(e => e.type)).toEqual(['notebook_snapshot']);
  });

  test('outputs emptied outside a run resync; emptied during a run do not', async () => {
    const h = await started(nb => {
      nb.insert(0, 'code', 'a');
    });
    h.nb.list[0].outputs = [{ output_type: 'stream', text: 'hi' }];
    await h.change(); // outputs appearing is not a structure change
    h.running.add(h.nb.list[0].id);
    h.nb.list[0].outputs = [];
    expect(await h.change()).toEqual([]);
    expect(h.resyncs).toBe(0);
    h.running.clear();
    h.nb.list[0].outputs = [{ output_type: 'stream', text: 'again' }];
    await h.change();
    h.nb.list[0].outputs = [];
    const produced = await h.change();
    expect(h.resyncs).toBe(1);
    expect(produced.map(e => e.type)).toEqual(['notebook_snapshot']);
  });

  test('reconciling before any snapshot asks for one', async () => {
    const h = new Harness();
    expect(h.reconciler.reconcile(h.nb, h.identity)).toEqual({ kind: 'resync', reason: 'not_started' });
  });
});

describe('builder', () => {
  test('default author is unknown with no confidence field (O2)', async () => {
    const h = await started(nb => nb.insert(0, 'code', 'x'));
    h.nb.insert(1, 'code', 'y');
    const [e] = await h.change();
    expect((e as any).authorHint).toBe('unknown');
    expect('authorConfidence' in e).toBe(false);
    expect((e as any).editor).toBe('jupyterlab');
    expect((e as any).originId).toBe('tab1');
    expect((e as any).links).toEqual({ cellId: (e as any).payload.cellId, cellRevision: 1 });
  });

  test('the metadata policy is applied: kernelspec and timing never reach an event', async () => {
    const h = await started(nb => nb.insert(0, 'code', 'x'));
    h.nb.nbMetadata = { kernelspec: { name: 'python3' }, title: 't' };
    h.nb.list[0].metadata = { trusted: true, execution: { 'iopub.execute_input': 'x' }, tags: ['a'] };
    const events = await h.change();
    const nbEvent = events.find(e => e.type === 'notebook_metadata_changed') as any;
    const cellEvent = events.find(e => e.type === 'cell_metadata_changed') as any;
    expect(nbEvent.payload.metadata).toEqual({ title: 't' });
    expect(cellEvent.payload.metadata).toEqual({ tags: ['a'] });
    // a change only in dropped keys is no change at all
    h.nb.nbMetadata = { kernelspec: { name: 'other' }, title: 't' };
    expect(await h.change()).toEqual([]);
    // the snapshot carries the policy-applied metadata too
    const snap = (await h.snapshot()) as any;
    expect(snap.payload.notebookMetadata).toEqual({ title: 't' });
    expect(snap.payload.cells[0].metadata).toEqual({ tags: ['a'] });
  });

  test('snapshot outputs go through prepareOutputs and form groups named snapshot:<eventId>', async () => {
    const nb = new FakeNotebook();
    nb.insert(0, 'code', 'x');
    nb.list[0].outputs = [{ output_type: 'stream', text: 'a' }, { output_type: 'error' }];
    nb.insert(1, 'markdown', 'md');
    const h = new Harness(nb);
    const snap = (await h.snapshot()) as any;
    expect(h.transport.prepared).toHaveLength(1);
    expect(snap.payload.cells[0].outputs).toHaveLength(1);
    expect(snap.payload.cells[0].outputs[0].runId).toBe(`snapshot:${snap.eventId}`);
    expect(snap.payload.cells[0].outputs[0].items).toHaveLength(2);
    expect(snap.payload.cells[1].outputs).toEqual([]);
  });

  test('a pasted cell with outputs carries prepared items', async () => {
    const h = await started();
    h.nb.insert(0, 'code', 'x');
    h.nb.list[0].outputs = [{ output_type: 'stream', text: 'a' }];
    const [e] = await h.change();
    expect((e as any).payload.outputs).toHaveLength(1);
    expect(h.transport.prepared).toHaveLength(1);
  });

  test('revisions continue from the server and never go down; +1 only when code changed', async () => {
    const nb = new FakeNotebook();
    nb.cellIdsStable = false;
    nb.insert(0, 'code', 'same');
    nb.insert(1, 'code', 'changed');
    nb.insert(2, 'code', 'fresh');
    const h = new Harness(nb);
    const [a, b] = nb.list.map(c => h.identity.blaymId(c.id));
    h.builder.noteStatus({ [a]: 7, [b]: 4, gone: 3 });
    h.builder.noteTip([
      { cellId: a, code: 'same' },
      { cellId: b, code: 'older' }
    ]);
    const snap = (await h.snapshot()) as any;
    expect(snap.payload.cells.map((c: any) => c.revision)).toEqual([7, 5, 1]);
    // a lower number from the server never lowers a known revision
    h.builder.noteStatus({ [a]: 2 });
    h.nb.list[0].source = 'same!';
    await h.change();
    await h.finish();
    const src = h.events.filter(e => e.type === 'cell_source_changed') as any[];
    expect(src[0].payload.revision).toBe(8);
    const resnap = (await h.snapshot()) as any;
    expect(resnap.payload.cells[0].revision).toBe(8);
    expect(status().cellRevisions).toEqual({});
  });

  test('lowering a revision is caught by the applier (planted fault)', () => {
    const a = new Applier();
    a.apply({ type: 'notebook_snapshot', payload: { path: 'p', notebookMetadata: {}, cells: [{ cellId: 'c', cellType: 'code', code: '', revision: 5, metadata: {}, outputs: [] }] } } as any);
    a.apply({ type: 'notebook_snapshot', payload: { path: 'p', notebookMetadata: {}, cells: [{ cellId: 'c', cellType: 'code', code: '', revision: 4, metadata: {}, outputs: [] }] } } as any);
    expect(a.problems).toEqual(['snapshot lowered revision of c']);
  });

  test('events are built in call order even when one waits for outputs', async () => {
    const h = await started();
    h.nb.insert(0, 'code', 'x');
    h.nb.list[0].outputs = [{ output_type: 'stream', text: 'a' }];
    h.nb.insert(1, 'code', 'y');
    const ev = await h.change();
    expect(ev.map(e => (e as any).payload.index)).toEqual([0, 1]);
  });
});
