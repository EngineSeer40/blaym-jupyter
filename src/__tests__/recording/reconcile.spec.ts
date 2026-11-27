import * as fc from 'fast-check';
import {
  Applier,
  appliedModel,
  applyOp,
  expectedModel,
  FakeNotebook,
  Harness,
  stepsArb
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
          for (; applied < h.events.length; applied++)
            applier.apply(h.events[applied]);
        };
        catchUp();
        for (const step of steps) {
          step.forEach(o => applyOp(h.nb, o));
          const produced = await h.change();
          await h.finish();
          catchUp();
          // the step's own events: no id both deleted and inserted
          const deleted = new Set(
            produced
              .filter(e => e.type === 'cell_deleted')
              .map(e => (e as any).payload.cellId)
          );
          produced
            .filter(e => e.type === 'cell_inserted')
            .forEach(e =>
              expect(deleted.has((e as any).payload.cellId)).toBe(false)
            );
          expect(appliedModel(applier)).toEqual(
            expectedModel(h.nb, h.identity)
          );
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
      fc.asyncProperty(
        fc.integer({ min: 2, max: 9 }),
        fc.nat(20),
        fc.nat(20),
        async (n, from, to) => {
          const h = await started(nb => {
            for (let i = 0; i < n; i++) nb.insert(i, 'code', `c${i}`);
          });
          applyOp(h.nb, { op: 'move', from, to });
          const produced = await h.change();
          const same = from % n === to % n;
          expect(produced.map(e => e.type)).toEqual(same ? [] : ['cell_moved']);
        }
      ),
      { numRuns: 200 }
    );
  });

  test('a type change is one cell_type_changed with the same id', async () => {
    const h = await started(nb => nb.insert(0, 'code', 'x'));
    h.nb.list[0].type = 'markdown';
    const produced = await h.change();
    expect(produced.map(e => e.type)).toEqual(['cell_type_changed']);
    expect((produced[0] as any).payload).toEqual({
      cellId: h.nb.list[0].id,
      cellType: 'markdown'
    });
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

  test('emptied outputs are not a change and never resync', async () => {
    const h = await started(nb => {
      nb.insert(0, 'code', 'a');
    });
    h.nb.list[0].outputs = [{ output_type: 'stream', text: 'hi' }];
    await h.change(); // outputs appearing is not a structure change
    h.nb.list[0].outputs = [];
    expect(await h.change()).toEqual([]);
    expect(h.resyncs).toBe(0);
  });
});

describe('builder', () => {
  test('events are built by the recorder rules', async () => {
    const h = await started(nb => nb.insert(0, 'code', 'x'));
    h.nb.nbMetadata = { kernelspec: { name: 'python3' }, title: 't' };
    h.nb.list[0].metadata = {
      trusted: true,
      execution: { 'iopub.execute_input': 'x' },
      tags: ['a']
    };
    h.nb.insert(1, 'code', 'pasted');
    h.nb.list[1].outputs = [{ output_type: 'stream', text: 'a' }];
    const events = await h.change();
    const pasted = events.find(e => e.type === 'cell_inserted') as any;
    expect(pasted.payload.cellId).toBeTruthy();
    expect(pasted.payload.outputs).toHaveLength(1);
    expect(h.transport.prepared).toHaveLength(1);
    expect((pasted as any).authorHint).toBe('unknown');
    expect('authorConfidence' in pasted).toBe(false);
    const nbEvent = events.find(
      e => e.type === 'notebook_metadata_changed'
    ) as any;
    const cellEvent = events.find(
      e => e.type === 'cell_metadata_changed'
    ) as any;
    expect(nbEvent.payload.metadata).toEqual({ title: 't' });
    expect(cellEvent.payload.metadata).toEqual({ tags: ['a'] });
    h.nb.list[0].source = 'x!';
    await h.change();
    await h.finish();
    const src = h.events.filter(e => e.type === 'cell_source_changed') as any[];
    expect(src[0].payload.revision).toBe(2);
  });
});
