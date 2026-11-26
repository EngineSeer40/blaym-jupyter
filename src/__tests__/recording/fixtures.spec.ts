/**
 * Cross-language recorder fixtures (S5 Task 14).
 *
 * Each scenario drives the real TypeScript recorder (reconciler, typing grouper,
 * event builder, outbox) over a fake notebook, a fake clock and a fake transport,
 * and records the `IngestRequest`s exactly as the recorder sent them, in order,
 * with the fake notebook's final state. `blaym_jupyter/tests/recording/
 * test_recorder_fixtures.py` ingests every file into the real Python service.
 *
 * The files are generated and are never edited by hand. This spec CHECKS that
 * each `fixtures/recorder/scenario-<name>.json` equals what the scenario writes
 * now, and fails with the name of the stale file. To regenerate after a change to
 * the recorder:
 *
 *   BLAYM_WRITE_FIXTURES=1 npx jest src/__tests__/recording/fixtures.spec.ts
 *
 * (Everything is deterministic: fake time, sequential ids, no randomness.)
 *
 * Two deliberate simplifications. The fake server accepts every event, so the
 * outbox would learn a made-up branch id and send it as `targetBranchId`; the
 * driver clears it after every pump, so the requests carry none and the service
 * uses the active branch. The fake `prepareOutputs` returns inline text items
 * (preview = the text, size = its bytes); the service hashes inline items itself.
 */
import type {
  IngestRequest,
  OutputItem,
  RecorderEvent
} from 'blaym-jupyter-types';
import {
  alignCells,
  applyPolicy,
  Outbox,
  TransportError
} from '../../recording';
import { accept, FakeNotebook, Harness, POLICY, settle } from './generate';

declare const require: (name: string) => any;
declare const __dirname: string;
declare const process: { env: Record<string, string | undefined> };
const { readFileSync, writeFileSync, readdirSync } = require('fs');
const { join } = require('path');
const DIR = join(__dirname, '..', '..', '..', 'fixtures', 'recorder');
const WRITE = process.env.BLAYM_WRITE_FIXTURES === '1';

interface FinalCell {
  id: string;
  type: string;
  code: string;
  metadata: Record<string, unknown>;
  /** The text of the cell's outputs, in order. */
  outputs: string[];
}
interface Scenario {
  name: string;
  description: string;
  requests: IngestRequest[];
  final: { cells: FinalCell[]; notebookMetadata: Record<string, unknown> };
}

function textItem(o: Record<string, unknown>, i: number): OutputItem {
  const text = String(o.text ?? o.data ?? '');
  return {
    outputType: (o.output_type as OutputItem['outputType']) ?? 'stream',
    mimeTypes: ['text/plain'],
    contentHash: `h${i}`,
    preview: text,
    truncated: false,
    sizeBytes: new TextEncoder().encode(text).length
  };
}
const out = (text: string) => ({ output_type: 'stream', name: 'stdout', text });

/** One recorder session: the harness, an outbox over its transport, and helpers. */
class Session {
  h: Harness;
  outbox: Outbox;
  private fed = 0;
  private viaOutbox = new Set<string>();

  constructor(
    readonly nb: FakeNotebook,
    readonly instanceId = 'tab1',
    session = 1
  ) {
    this.h = new Harness(nb);
    // event ids must be UUIDs; these are deterministic, and differ between sessions
    let n = 0;
    this.h.ids.uuid = () =>
      `00000000-0000-4000-8000-${session.toString(16).padStart(4, '0')}${(++n).toString(16).padStart(8, '0')}`;
    this.h.transport.prepareOutputs = (req: {
      outputs: Record<string, unknown>[];
    }) => {
      this.h.transport.prepared.push(req.outputs);
      return Promise.resolve({
        apiVersion: 1 as const,
        items: req.outputs.map(textItem)
      });
    };
    this.outbox = new Outbox({
      transport: this.h.transport,
      scheduler: this.h.time,
      clock: this.h.time,
      explorationId: 'nb1',
      editor: 'jupyterlab',
      instanceId,
      onBranch: () => this.outbox.retarget(undefined),
      snapshotter: async () => {
        const status = await this.h.transport.status();
        this.h.builder.noteStatus(status.cellRevisions);
        const ev = await this.h.snapshot();
        this.viaOutbox.add((ev as { eventId: string }).eventId);
        return ev;
      }
    });
  }

  get requests(): IngestRequest[] {
    return this.h.transport.requests;
  }

  /** Hand everything built so far to the outbox and let the fake network answer. */
  async pump(ms = 0): Promise<void> {
    await (this.h as any).settleQueue();
    const events: RecorderEvent[] = this.h.events;
    for (; this.fed < events.length; this.fed++) {
      const id = (events[this.fed] as { eventId: string }).eventId;
      if (!this.viaOutbox.has(id)) {
        this.outbox.enqueue(events[this.fed]);
      }
    }
    for (let i = 0; i < 6; i++) {
      await settle(50);
      this.h.time.advance(ms / 6);
      await settle(50);
    }
  }

  /** The first snapshot, as the adapter sends it after `register`. */
  async start(): Promise<void> {
    const ev = await this.h.snapshot();
    this.viaOutbox.add((ev as { eventId: string }).eventId);
    this.outbox.enqueue(ev);
    await this.pump();
  }

  /** Change the notebook, reconcile, wait out the typing pause, deliver. */
  async step(change: (nb: FakeNotebook) => void, wait = 3000): Promise<void> {
    change(this.nb);
    await this.h.change();
    await this.pump(wait);
  }

  /** A run starts: the notebook may change at once. */
  async run(
    _index: number,
    change?: (nb: FakeNotebook) => void
  ): Promise<void> {
    await this.step(change ?? (() => undefined), 0);
  }

  async end(): Promise<void> {
    await this.h.finish();
    await this.pump(3000);
  }

  final(): Scenario['final'] {
    return {
      cells: this.nb.list.map(c => ({
        id: this.h.identity.blaymId(c.id),
        type: c.type,
        code: c.source,
        metadata: applyPolicy(c.metadata, 'cell', POLICY).metadata,
        outputs: c.outputs.map(o =>
          String((o as { text?: unknown }).text ?? '')
        )
      })),
      notebookMetadata: applyPolicy(
        this.nb.notebookMetadata(),
        'notebook',
        POLICY
      ).metadata
    };
  }
}

function notebook(prefix = 'c', stable = true): FakeNotebook {
  const nb = new FakeNotebook();
  nb.cellIdsStable = stable;
  let n = 0;
  nb.freshId = () => `${prefix}${++n}`;
  return nb;
}

function seed(nb: FakeNotebook): void {
  nb.nbMetadata = { kernelspec: { name: 'python3' }, x: 1 };
  nb.insert(0, 'code', 'import pandas as pd');
  nb.insert(1, 'markdown', '# Title');
  nb.insert(2, 'code', 'df.head()');
  nb.list[0].outputs = [out('loaded\n')];
  nb.list[2].outputs = [out('a b\n'), out('1 2\n')];
}

type Build = () => Promise<{
  requests: IngestRequest[];
  final: Scenario['final'];
}>;

const done = async (s: Session) => ({
  requests: s.requests.slice(),
  final: s.final()
});

const scenarios: Array<{ name: string; description: string; build: Build }> = [
  {
    name: 'first-snapshot',
    description:
      'The first snapshot of a notebook with three cells, two with outputs, then nothing.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      await s.start();
      await s.end();
      return done(s);
    }
  },
  {
    name: 'typing-and-run',
    description:
      'Typing in one cell grouped into steps, then a run flushes the typing before it.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      await s.start();
      await s.step(n => (n.list[0].source = 'import pandas as pd\nx'));
      await s.step(n => (n.list[0].source = 'import pandas as pd\nx = 1'));
      await s.step(n => (n.list[2].source = 'df.he'), 0);
      await s.run(2, n => (n.list[2].source = 'df.head(3)'));
      await s.end();
      return done(s);
    }
  },
  {
    name: 'insert-delete-move',
    description:
      'Insert a cell, move it, delete another, change a cell type and cell metadata.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      await s.start();
      await s.step(n => n.insert(1, 'code', 'y = 2'));
      await s.step(n => n.move(1, 3));
      await s.step(n => n.delete(0));
      await s.step(n => (n.list[1].type = 'raw'));
      await s.step(n => (n.list[0].metadata = { tags: ['parameters'] }));
      await s.step(n => (n.nbMetadata = { ...n.nbMetadata, x: 2 }));
      await s.end();
      return done(s);
    }
  },
  {
    name: 'paste-with-outputs',
    description:
      'Cells pasted in with their outputs, in the middle and at the end.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      await s.start();
      await s.step(n => {
        const id = n.insert(1, 'code', 'print("pasted")');
        n.list.find(c => c.id === id)!.outputs = [out('pasted\n')];
      });
      await s.step(n => {
        const id = n.insert(4, 'code', 'z = 3');
        n.list.find(c => c.id === id)!.outputs = [out('3\n'), out('three\n')];
      });
      await s.end();
      return done(s);
    }
  },
  {
    name: 'revert-resync',
    description:
      'The model is replaced (a revert): every id changes, a resync snapshot is sent.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      await s.start();
      await s.step(n => (n.list[0].source = 'edited'));
      await s.step(n => {
        n.reload();
        n.list[0].source = 'reverted';
      });
      if (s.h.resyncs === 0)
        throw new Error('the revert did not produce a resync');
      await s.step(n => (n.list[1].source = '# after the revert'));
      await s.end();
      return done(s);
    }
  },
  {
    name: 'takeover-resync',
    description:
      'Another tab recorded in between: this tab is asked for a resync and sends a snapshot.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      await s.start();
      await s.step(n => (n.list[0].source = 'before'));
      s.h.time.advance(6000);
      s.outbox.requestResync('takeover');
      await s.pump(6000);
      await s.step(n => n.insert(3, 'markdown', 'after the takeover'));
      await s.end();
      return done(s);
    }
  },
  {
    name: 'delivery-failure-retry',
    description:
      'The server answers 503 once and the network drops once: the same events are sent again.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      s.h.transport.ingestHandler = (req, call) =>
        call === 1
          ? Promise.reject(new TransportError('unavailable', 'http', 503))
          : call === 3
            ? Promise.reject(new TransportError('offline', 'network'))
            : Promise.resolve(accept(req));
      await s.start();
      await s.step(n => (n.list[0].source = 'one'));
      await s.step(n => n.insert(1, 'code', 'two'), 6000);
      await s.step(n => n.delete(2), 6000);
      await s.end();
      await s.pump(20000);
      return done(s);
    }
  },
  {
    name: 'duplicate-delivery',
    description:
      'The server stored a batch but the answer was lost: the batch is sent again, all duplicates.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      await s.start();
      // the first response to the next request is lost after the server stored it
      let lost = true;
      s.h.transport.ingestHandler = req => {
        if (lost) {
          lost = false;
          return Promise.reject(new TransportError('lost', 'network'));
        }
        return Promise.resolve(accept(req));
      };
      await s.step(n => n.insert(0, 'code', 'a = 1'), 6000);
      await s.step(n => (n.list[1].source = 'changed'), 6000);
      await s.end();
      await s.pump(20000);
      return done(s);
    }
  },
  {
    name: 'reopen-with-aliases',
    description:
      'A 4.4-style notebook reopened: fresh random model ids are mapped to the recorded Blaym ids.',
    build: async () => {
      const nb1 = notebook('m', false);
      seed(nb1);
      const s1 = new Session(nb1, 'tab1', 1);
      await s1.start();
      await s1.step(n => (n.list[2].source = 'df.tail()'));
      await s1.end();
      const tip = nb1.list.map(c => ({
        cellId: s1.h.identity.blaymId(c.id),
        type: c.type,
        code: c.source
      }));
      const revisions: Record<string, number> = {};
      tip.forEach(
        c => (revisions[c.cellId] = s1.h.builder.revisionOf(c.cellId) ?? 1)
      );

      // the page is opened again: the file was written without ids, so the editor invents new ones
      const nb2 = notebook('r', false);
      nb2.nbMetadata = nb1.nbMetadata;
      nb2.list = nb1.list.map(c => ({
        ...c,
        id: nb2.freshId(),
        outputs: c.outputs.slice()
      }));
      // the user changed a cell while the page was closed (the file was edited elsewhere): alignment still matches by position
      nb2.list[1].source = '# A new title';
      const s2 = new Session(nb2, 'tab2', 2);
      const align = alignCells(
        nb2.list.map(c => ({ id: c.id, type: c.type, source: c.source })),
        tip
      );
      Object.entries(align.matches).forEach(([model, blaym]) =>
        s2.h.identity.setAlias(model, blaym)
      );
      s2.h.builder.noteStatus(revisions);
      await s2.start();
      await s2.step(n => n.insert(3, 'code', 'new_cell = 1'));
      await s2.step(n => (n.list[0].source = 'import numpy as np'));
      await s2.end();
      return {
        requests: [...s1.requests, ...s2.requests],
        final: s2.final()
      };
    }
  },
  {
    name: 'many-edits-and-moves',
    description:
      'A longer mixed session: several cells edited between moves, deletes and inserts.',
    build: async () => {
      const nb = notebook();
      seed(nb);
      const s = new Session(nb);
      await s.start();
      for (let i = 0; i < 4; i++) {
        await s.step(n => (n.list[i % n.list.length].source += `\n# ${i}`));
        await s.step(n => n.insert(i, 'code', `v${i} = ${i}`));
        await s.step(n => n.move(0, n.list.length - 1));
      }
      await s.step(n => n.delete(1));
      await s.run(0);
      await s.end();
      return done(s);
    }
  }
];

describe('recorder fixtures: the files are what the scenarios write now', () => {
  test.each(scenarios.map(s => [s.name, s] as const))(
    'scenario %s',
    async (name, sc) => {
      const built = await sc.build();
      expect(built.requests.length).toBeGreaterThan(0);
      const scenario: Scenario = {
        name,
        description: sc.description,
        ...built
      };
      const text = JSON.stringify(scenario, null, 2) + '\n';
      const file = join(DIR, `scenario-${name}.json`);
      if (WRITE) {
        writeFileSync(file, text);
        return;
      }
      let onDisk = '';
      try {
        onDisk = readFileSync(file, 'utf8');
      } catch {
        /* missing: reported below */
      }
      if (onDisk !== text) {
        throw new Error(
          `fixtures/recorder/scenario-${name}.json is stale or missing; regenerate with BLAYM_WRITE_FIXTURES=1 npx jest src/__tests__/recording/fixtures.spec.ts`
        );
      }
    }
  );

  test('no scenario file is left without a scenario', () => {
    const files = (readdirSync(DIR) as string[])
      .filter(f => /^scenario-.*\.json$/.test(f))
      .sort();
    expect(files).toEqual(scenarios.map(s => `scenario-${s.name}.json`).sort());
  });
});
