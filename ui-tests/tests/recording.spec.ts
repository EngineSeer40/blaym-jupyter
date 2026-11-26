// Task 16: the recorder in a real JupyterLab page. Needs the server of the runner
// (env BLAYM_ROOT = the server's root folder, BLAYM_TOKEN). Everything is read through the page
// (window.jupyterapp, exposed by the test server) and the Blaym API routes.
import { expect, test } from '@jupyterlab/galata';
import * as fs from 'fs';
import * as path from 'path';

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

const ROOT = process.env.BLAYM_ROOT as string;
const NB = 'rec16.ipynb';
const API = '/blaym-jupyter/api';

test.use({
  autoGoto: false,
  waitForApplication: async ({}, use) => {
    await use(async () => undefined);
  }
});

function writeNb(rel: string, sources: string[]): void {
  const nb = {
    cells: sources.map((src, i) => ({
      cell_type: 'code',
      id: `rec-cell-${i}`,
      metadata: {},
      source: src,
      execution_count: null,
      outputs: []
    })),
    metadata: {
      kernelspec: {
        display_name: 'Python 3',
        language: 'python',
        name: 'python3'
      },
      language_info: { name: 'python' }
    },
    nbformat: 4,
    nbformat_minor: 5
  };
  fs.writeFileSync(path.join(ROOT, rel), JSON.stringify(nb, null, 1));
}

async function openNb(page: Any, baseURL: string): Promise<void> {
  await page.goto(`${baseURL}/lab/tree/${NB}`);
  await page.waitForFunction(
    (r: string) =>
      (window as Any).jupyterapp?.shell?.currentWidget?.context?.path === r &&
      !!(window as Any).jupyterapp.shell.currentWidget.model?.cells?.length,
    NB,
    { timeout: 40000 }
  );
}

const cmd = (p: Any, c: string) =>
  p.evaluate((x: string) => (window as Any).jupyterapp.commands.execute(x), c);

const model = (p: Any) =>
  p.evaluate(() =>
    (window as Any).jupyterapp.shell.currentWidget.model.sharedModel.cells.map(
      (c: Any) => ({
        id: c.getId(),
        type: c.cell_type,
        src: c.getSource()
      })
    )
  );

const active = (p: Any, i: number) =>
  p.evaluate((n: number) => {
    (window as Any).jupyterapp.shell.currentWidget.content.activeCellIndex = n;
  }, i);

const toolbarState = (p: Any) =>
  p.locator('.blaym-toggle').first().getAttribute('data-blaym-state');
const barState = (p: Any) =>
  p.locator('.blaym-status').first().getAttribute('data-blaym-state');

test('recording in a real page', async ({
  page: galata,
  context,
  baseURL,
  request
}) => {
  test.setTimeout(280000);
  const page = galata.page;
  page.on('dialog', d => void d.accept());
  writeNb(NB, ["print('hello')", 'x = 1', 'y = 2']);
  const before = fs.readFileSync(path.join(ROOT, NB));
  const evidence: Record<string, unknown> = {};

  const registers: Any[] = [];
  const keepalive: Array<{ url: string; status: number }> = [];
  page.on('response', async r => {
    const u = r.url();
    if (u.includes(`${API}/notebooks/register`) && r.ok()) {
      registers.push(await r.json().catch(() => undefined));
    }
    if (u.includes(`${API}/notebooks/close`) || u.includes(`${API}/ingest`)) {
      keepalive.push({ url: u.split(API)[1], status: r.status() });
    }
  });
  const sentTypes: string[] = [];
  page.on('request', r => {
    if (r.url().includes(`${API}/ingest`)) {
      for (const e of r.postDataJSON()?.events ?? []) {
        sentTypes.push(e.type);
      }
    }
  });
  const consoleLines: string[] = [];
  page.on('console', m => {
    if (m.type() === 'error') {
      consoleLines.push(m.text());
    }
  });

  await openNb(page, baseURL!);
  // collaboration property in a plain server: the notebook model reports it false
  evidence.modelCollaborative = await page.evaluate(
    () =>
      (window as Any).jupyterapp.shell.currentWidget.context.model.collaborative
  );
  expect(evidence.modelCollaborative).toBe(false);

  // recording is off until switched on
  await expect.poll(() => toolbarState(page), { timeout: 20000 }).toBe('off');
  expect(await barState(page)).toBe('off');

  // instrument the real page: kernel messages, cell change signals
  await page.evaluate(() => {
    const w = (window as Any).jupyterapp.shell.currentWidget;
    const log: Any = { messages: [], cellChanges: [] };
    (window as Any).__ev = log;
    const hook = () => {
      const k = w.context.sessionContext.session?.kernel;
      if (!k || (window as Any).__hooked === k) {
        return;
      }
      (window as Any).__hooked = k;
      k.anyMessage.connect((_: Any, a: Any) => {
        log.messages.push({
          direction: a.direction,
          type: a.msg.header.msg_type,
          msgKeys: Object.keys(a.msg).sort(),
          metadata: a.msg.metadata,
          parent: a.msg.parent_header?.msg_id
        });
      });
    };
    hook();
    w.context.sessionContext.kernelChanged.connect(hook);
    w.context.sessionContext.connectionStatusChanged.connect(hook);
    w.model.sharedModel.cells.forEach((c: Any) =>
      c.changed.connect((_: Any, ch: Any) =>
        log.cellChanges.push(Object.keys(ch).sort())
      )
    );
  });

  // the file is untouched by switching recording on
  await page.locator('.blaym-toggle').first().click();
  await expect
    .poll(() => toolbarState(page), { timeout: 20000 })
    .toBe('recording');
  expect(await barState(page)).toBe('recording');
  await page.waitForTimeout(3000);
  expect(fs.readFileSync(path.join(ROOT, NB)).equals(before)).toBe(true);

  // the notice for a first registration: none for a new notebook
  expect(registers.length).toBeGreaterThan(0);
  evidence.firstRegisterNotices = registers[0]?.notices;
  expect(registers[0]?.notices ?? []).toEqual([]);
  const explorationId: string = registers[registers.length - 1].explorationId;

  const stateMatches = async (label: string, p: Any = page): Promise<void> => {
    const want = await model(p);
    await expect
      .poll(
        async () => {
          const o = await (
            await request.get(`${API}/outline?explorationId=${explorationId}`)
          ).json();
          if (!o.branches) {
            return 'no history yet';
          }
          const s = await (
            await request.get(
              `${API}/state?explorationId=${explorationId}&branchId=${o.rootBranchId}&tick=${o.highestTick}`
            )
          ).json();
          if (s.status !== 'ok') {
            return `status ${s.status}`;
          }
          return s.state.notebook.cells.map((c: Any) => ({
            id: c.cellId,
            type: c.cellType,
            src: c.code
          }));
        },
        { message: label, timeout: 15000, intervals: [400] }
      )
      .toEqual(want);
  };
  await stateMatches('initial snapshot');

  // add a cell and type into it (typing grouped)
  await active(page, 2);
  await cmd(page, 'notebook:insert-cell-below');
  await page.keyboard.press('Enter');
  await page.keyboard.type('z = 3', { delay: 60 });
  await page.keyboard.press('Escape');
  await page.waitForTimeout(1500);
  await stateMatches('added and typed');

  // an unsaved edit (setSource without save) of an existing cell
  await page.evaluate(() => {
    (
      window as Any
    ).jupyterapp.shell.currentWidget.model.sharedModel.cells[1].setSource(
      'x = 100'
    );
  });
  await page.waitForTimeout(1500);
  await stateMatches('unsaved edit');

  // move
  await active(page, 3);
  await cmd(page, 'notebook:move-cell-up');
  await page.waitForTimeout(1500);
  await stateMatches('moved');

  // type change
  const earlySnapshots = sentTypes.filter(
    t => t === 'notebook_snapshot'
  ).length;
  await active(page, 0);
  await cmd(page, 'notebook:change-cell-to-markdown');
  await page.waitForTimeout(1500);
  await stateMatches('type changed');
  await cmd(page, 'notebook:change-cell-to-code');
  await page.waitForTimeout(1500);
  await stateMatches('type changed back');
  expect(sentTypes.filter(t => t === 'cell_type_changed').length).toBe(2);
  expect(sentTypes.filter(t => t === 'notebook_snapshot').length).toBe(
    earlySnapshots
  );

  // delete
  await active(page, 1);
  await cmd(page, 'notebook:delete-cell');
  await page.waitForTimeout(1500);
  await stateMatches('deleted');

  // run a cell
  await active(page, 0);
  await cmd(page, 'notebook:run-cell');
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            (window as Any).jupyterapp.shell.currentWidget.content.widgets[0]
              .model.outputs.length
        ),
      { timeout: 60000 }
    )
    .toBeGreaterThan(0);
  await page.waitForTimeout(2500);
  await stateMatches('after run');
  const o = await (
    await request.get(`${API}/outline?explorationId=${explorationId}`)
  ).json();
  const st = await (
    await request.get(
      `${API}/state?explorationId=${explorationId}&branchId=${o.rootBranchId}&tick=${o.highestTick}`
    )
  ).json();
  expect(st.state.runs.length).toBeGreaterThan(0);
  expect(st.state.notebook.cells[0].outputs.length).toBeGreaterThan(0);

  // outputs in the real page against the rebuilt state (S5 fix, LT-1, LT-2, LT-7, LT-8)
  const snapshotsBefore = sentTypes.filter(
    t => t === 'notebook_snapshot'
  ).length;
  expect(snapshotsBefore).toBeGreaterThan(0);

  const latestState = async (): Promise<Any> => {
    const oc = await (
      await request.get(`${API}/outline?explorationId=${explorationId}`)
    ).json();
    return (
      await (
        await request.get(
          `${API}/state?explorationId=${explorationId}&branchId=${oc.rootBranchId}&tick=${oc.highestTick}`
        )
      ).json()
    ).state;
  };
  const joined = (t: Any): string =>
    Array.isArray(t) ? t.join('') : String(t);
  const liveOutputs = async (): Promise<Array<Array<[string, string]>>> => {
    const raw: Any[][] = await page.evaluate(() =>
      (
        window as Any
      ).jupyterapp.shell.currentWidget.model.sharedModel.cells.map((c: Any) =>
        c.cell_type === 'code' ? c.toJSON().outputs : []
      )
    );
    return raw.map(outs =>
      outs.map((o: Any): [string, string] => [
        o.output_type,
        o.output_type === 'stream'
          ? joined(o.text)
          : o.output_type === 'error'
            ? o.traceback.join('\n')
            : joined(o.data['text/plain'])
      ])
    );
  };
  const recordedOutputs = async () =>
    (await latestState()).notebook.cells.map((c: Any) =>
      c.outputs.flatMap((g: Any) =>
        g.items.map((x: Any) => [x.outputType, x.preview])
      )
    );
  const shown: Record<string, unknown> = {};
  evidence.outputsByCase = shown;
  // `kept`: cells where the history is expected to differ from the page, with what it holds
  const sameOutputs = async (
    label: string,
    kept: Record<number, unknown> = {}
  ): Promise<void> => {
    await page.waitForTimeout(2500);
    const live = await liveOutputs();
    const short = (v: unknown) => JSON.stringify(v).slice(0, 300);
    console.log(
      `CASE ${label}\n  live     ${short(live)}\n  recorded ${short(await recordedOutputs())}`
    );
    shown[label] = live;
    await expect
      .poll(recordedOutputs, {
        message: label,
        timeout: 15000,
        intervals: [500]
      })
      .toEqual(live.map((c, i) => kept[i] ?? c));
    await stateMatches(label);
  };
  const setSource = async (i: number, src: string): Promise<void> => {
    await page.evaluate(
      ([n, v]: [number, string]) =>
        (window as Any).jupyterapp.shell.currentWidget.model.sharedModel.cells[
          n
        ].setSource(v),
      [i, src] as [number, string]
    );
    await page.waitForTimeout(1500);
  };
  const idle = async (text: string, cell: number): Promise<void> => {
    await expect
      .poll(async () => JSON.stringify((await liveOutputs())[cell]), {
        timeout: 60000
      })
      .toContain(text);
  };
  const runAt = async (i: number): Promise<void> => {
    await active(page, i);
    await cmd(page, 'notebook:run-cell');
  };

  await setSource(0, "print('first')");
  await runAt(0);
  await idle('first', 0);
  await sameOutputs('first run');

  await setSource(0, "print('again')");
  await runAt(0);
  await idle('again', 0);
  await sameOutputs('re-run of a cell with output');

  await setSource(1, "print('old output')");
  await runAt(1);
  await idle('old output', 1);
  await setSource(
    0,
    "import time\nfor i in range(8):\n    print('step', i, flush=True)\n    time.sleep(0.7)"
  );
  await setSource(1, "print('new output')");
  await runAt(0);
  await runAt(1);
  await idle('new output', 1);
  await sameOutputs('long run with a cell queued behind');

  await setSource(2, "print('third, before')");
  await runAt(2);
  await idle('third, before', 2);
  await sameOutputs('third cell run');
  const runsOf = async (i: number): Promise<number> => {
    const st = await latestState();
    const id = st.notebook.cells[i].cellId;
    return st.runs.filter((r: Any) => r.cellId === id).length;
  };
  const skippedRuns = await runsOf(2);
  await setSource(1, "raise ValueError('boom')");
  await cmd(page, 'notebook:run-all-cells');
  await idle('boom', 1);
  await page.waitForTimeout(6000);
  // The page empties the queued cell although the kernel never ran it. No run is recorded
  // for it (LT-8), so the history keeps the output of its last real run.
  expect((await liveOutputs())[2]).toEqual([]);
  const keptThird = { 2: [['stream', 'third, before\n']] };
  await sameOutputs('run all with an error', keptThird);
  expect(await runsOf(2)).toBe(skippedRuns);
  // the cell after the skipped one is not held up
  await setSource(2, "print('after the error')");
  await runAt(2);
  await idle('after the error', 2);
  await sameOutputs('run after the error');

  const typesBefore = sentTypes.filter(t => t === 'cell_type_changed').length;
  await active(page, 0);
  await cmd(page, 'notebook:change-cell-to-markdown');
  await sameOutputs('code to markdown');
  await cmd(page, 'notebook:change-cell-to-code');
  await sameOutputs('markdown back to code');
  evidence.typeChangeEvents =
    sentTypes.filter(t => t === 'cell_type_changed').length - typesBefore;
  expect(evidence.typeChangeEvents).toBe(2);
  expect(sentTypes.filter(t => t === 'notebook_snapshot').length).toBe(
    snapshotsBefore
  );
  expect(await barState(page)).toBe('recording');

  // Clear Outputs is its own event, sent only for the user's clear commands
  const clearedCount = (): number =>
    sentTypes.filter(t => t === 'cell_outputs_cleared').length;
  await page.evaluate(() => {
    const w = window as Any;
    w.__commands = [];
    w.jupyterapp.commands.commandExecuted.connect((_: Any, a: Any) =>
      w.__commands.push(a.id)
    );
  });
  const commandsSeen = (): Promise<string[]> =>
    page.evaluate(() => (window as Any).__commands.splice(0));
  // Not awaited: the command waits for the restart dialog to be answered.
  const restartWith = async (command: string): Promise<void> => {
    await page.evaluate((c: string) => {
      void (window as Any).jupyterapp.commands.execute(c);
    }, command);
    await page.locator('.jp-Dialog button.jp-mod-accept').click();
  };
  const clearEvidence: Record<string, unknown> = {};
  evidence.clearCommands = clearEvidence;
  const noted = async (label: string, from: number): Promise<void> => {
    clearEvidence[label] = {
      commands: await commandsSeen(),
      events: clearedCount() - from
    };
  };

  await setSource(1, "print('second again')");
  await runAt(1);
  await idle('second again', 1);
  expect(clearedCount()).toBe(0);

  await active(page, 2);
  await cmd(page, 'notebook:clear-cell-output');
  // cell 0 has no output since its type change, so only cell 2 differs
  await sameOutputs('clear one cell');
  await noted('clear one cell', 0);
  expect(clearedCount()).toBe(1);

  await runAt(2);
  await idle('after the error', 2);
  await sameOutputs('run after clear');
  await commandsSeen();
  let from = clearedCount();
  await cmd(page, 'notebook:clear-all-cell-outputs');
  await sameOutputs('clear all');
  await noted('clear all', from);
  // cells 1 and 2 had output
  expect(clearedCount() - from).toBe(2);

  await runAt(2);
  await idle('after the error', 2);
  await sameOutputs('run before restart and clear');
  await commandsSeen();
  from = clearedCount();
  await restartWith('notebook:restart-clear-output');
  await expect.poll(() => clearedCount() - from, { timeout: 30000 }).toBe(1);
  await sameOutputs('restart and clear');
  await noted('restart and clear', from);
  expect((await liveOutputs()).flat()).toEqual([]);
  expect(sentTypes.filter(t => t === 'notebook_snapshot').length).toBe(
    snapshotsBefore
  );
  expect(await barState(page)).toBe('recording');
  // Restart and run all sends no clear command and no clear event. Only that is
  // asserted: in this long scenario the page has been seen to stay "restarting".
  from = clearedCount();
  await restartWith('notebook:restart-run-all');
  await page.waitForTimeout(8000);
  await noted('restart and run all', from);
  expect(clearedCount()).toBe(from);
  clearEvidence['after restart and run all'] = {
    kernel: await page.evaluate(
      () =>
        (window as Any).jupyterapp.shell.currentWidget.sessionContext.session
          ?.kernel?.status
    ),
    outputs: (await liveOutputs()).map(o => o.length)
  };
  console.log('CLEAR ' + JSON.stringify(clearEvidence));

  // what the real page showed (S5 proofs)
  const ev = await page.evaluate(() => (window as Any).__ev);
  const exec = ev.messages.filter(
    (m: Any) => m.direction === 'send' && m.type === 'execute_request'
  );
  evidence.executeRequestMetadata = exec.map((m: Any) => m.metadata);
  evidence.anyMessageKeys = ev.messages[0]?.msgKeys;
  evidence.messageTypes = [
    ...new Set(ev.messages.map((m: Any) => `${m.direction}:${m.type}`))
  ];
  evidence.cellChangeKinds = [
    ...new Set(ev.cellChanges.map((k: string[]) => k.join('+')))
  ];
  expect(exec.length).toBeGreaterThan(0);
  expect(typeof exec[0].metadata?.cellId).toBe('string');

  // the file is still unmodified by Blaym (nothing saved by us), and a save writes no Blaym key
  expect(fs.readFileSync(path.join(ROOT, NB)).equals(before)).toBe(true);
  await cmd(page, 'docmanager:save');
  await page.waitForTimeout(1500);
  expect(
    fs.readFileSync(path.join(ROOT, NB), 'utf8').toLowerCase()
  ).not.toContain('blaym');

  // second tab: another tab is recording, records nothing
  const tickBefore = (
    await (
      await request.get(`${API}/outline?explorationId=${explorationId}`)
    ).json()
  ).highestTick;
  const page2 = await context.newPage();
  await openNb(page2, baseURL!);
  await expect
    .poll(() => barState(page2), { timeout: 20000 })
    .toBe('other-tab');
  expect(await toolbarState(page2)).toBe('other-tab');
  await expect(
    page2.locator('.jp-Notification-Toast-Container, .Toastify')
  ).toContainText(/Another tab is recording/, { timeout: 10000 });
  await page2.evaluate(() => {
    (
      window as Any
    ).jupyterapp.shell.currentWidget.model.sharedModel.cells[0].setSource(
      'tab2 edit'
    );
  });
  await page2.waitForTimeout(2500);
  const tickAfter = (
    await (
      await request.get(`${API}/outline?explorationId=${explorationId}`)
    ).json()
  ).highestTick;
  expect(tickAfter).toBe(tickBefore);

  // the first tab goes away cleanly (pagehide: the keepalive batch and the close call)
  const nKeep = keepalive.length;
  await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  await expect
    .poll(() => keepalive.length, { timeout: 10000 })
    .toBeGreaterThan(nKeep);
  evidence.keepalive = keepalive.slice(nKeep);
  expect(
    keepalive.slice(nKeep).every(k => k.status >= 200 && k.status < 300)
  ).toBe(true);
  await page.close();

  // tab 2 takes over, resyncs (its own model is the record from then on)
  await expect
    .poll(() => barState(page2), { timeout: 60000 })
    .toBe('recording');
  await stateMatches('takeover resync', page2);
  expect((await model(page2))[0].src).toBe('tab2 edit');

  fs.writeFileSync(
    path.join(ROOT, 'evidence.json'),
    JSON.stringify(
      { ...evidence, consoleErrors: consoleLines.slice(0, 10) },
      null,
      2
    )
  );
  console.log('EVIDENCE ' + JSON.stringify(evidence));
});
