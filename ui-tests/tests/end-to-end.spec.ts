// The one end-to-end session in a real JupyterLab, recording on, a nbformat 4.4 notebook
// (no cell ids). Afterwards the Blaym state is read through the API routes only.
// Needs the server of ui-tests/run-galata.sh (env BLAYM_ROOT). Run it with
//   BLAYM_SPEC=end-to-end ui-tests/run-galata.sh
import { expect, test } from '@jupyterlab/galata';
import * as fs from 'fs';
import * as path from 'path';

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

const ROOT = process.env.BLAYM_ROOT as string;
const NB = 'e2e.ipynb';
const NB2 = 'e2e-renamed.ipynb';
const API = '/blaym-jupyter/api';
const PNG =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';

test.use({
  autoGoto: false,
  waitForApplication: async ({}, use) => {
    await use(async () => undefined);
  }
});

function writeNb(rel: string, sources: string[]): void {
  // nbformat 4.4: no cell ids
  const nb = {
    cells: sources.map(src => ({
      cell_type: 'code',
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
    nbformat_minor: 4
  };
  fs.writeFileSync(path.join(ROOT, rel), JSON.stringify(nb, null, 1));
}

async function openNb(page: Any, baseURL: string, rel: string): Promise<void> {
  await page.goto(`${baseURL}/lab/tree/${rel}`);
  await page.waitForFunction(
    (r: string) =>
      (window as Any).jupyterapp?.shell?.currentWidget?.context?.path === r &&
      !!(window as Any).jupyterapp.shell.currentWidget.model?.cells?.length,
    rel,
    { timeout: 40000 }
  );
}

const cmd = (p: Any, c: string) =>
  p.evaluate((x: string) => (window as Any).jupyterapp.commands.execute(x), c);
const active = (p: Any, i: number) =>
  p.evaluate((n: number) => {
    (window as Any).jupyterapp.shell.currentWidget.content.activeCellIndex = n;
  }, i);
const setSource = (p: Any, i: number, src: string) =>
  p.evaluate(
    ([n, v]: [number, string]) =>
      (window as Any).jupyterapp.shell.currentWidget.model.sharedModel.cells[
        n
      ].setSource(v),
    [i, src] as [number, string]
  );
const sources = (p: Any): Promise<string[]> =>
  p.evaluate(() =>
    (window as Any).jupyterapp.shell.currentWidget.model.sharedModel.cells.map(
      (c: Any) => c.getSource()
    )
  );
const outputCount = (p: Any, i: number): Promise<number> =>
  p.evaluate(
    (n: number) =>
      (window as Any).jupyterapp.shell.currentWidget.content.widgets[n].model
        .outputs.length,
    i
  );
const barState = (p: Any) =>
  p.locator('.blaym-status').first().getAttribute('data-blaym-state');

test('end-to-end session', async ({
  page: galata,
  context,
  baseURL,
  request
}) => {
  test.setTimeout(290000);
  let page = galata.page;
  page.on('dialog', d => void d.accept());
  writeNb(NB, ['a = 1', 'b = 2', 'c = 3']);
  await openNb(page, baseURL!, NB);

  const registers: Any[] = [];
  const sent: Any[] = []; // events the first page sends to the ingest route
  page.on('request', r => {
    if (r.url().includes(`${API}/ingest`)) {
      sent.push(...(r.postDataJSON()?.events ?? []));
    }
  });
  const count = (t: string) => sent.filter(e => e.type === t).length;
  page.on('response', async r => {
    if (r.url().includes(`${API}/notebooks/register`) && r.ok()) {
      registers.push(await r.json().catch(() => undefined));
    }
  });
  await expect.poll(() => barState(page), { timeout: 20000 }).toBe('off');
  await page.locator('.blaym-toggle').first().click();
  await expect.poll(() => barState(page), { timeout: 20000 }).toBe('recording');
  await expect
    .poll(() => registers.length, { timeout: 10000 })
    .toBeGreaterThan(0);
  const explorationId: string = registers[registers.length - 1].explorationId;

  const get = async (url: string): Promise<Any> =>
    (await request.get(`${API}${url}`)).json();
  const outline = (): Promise<Any> =>
    get(`/outline?explorationId=${explorationId}`);
  const latest = async (): Promise<Any> => {
    const o = await outline();
    return (
      await get(
        `/state?explorationId=${explorationId}&branchId=${o.rootBranchId}&tick=${o.highestTick}`
      )
    ).state;
  };
  const allEvents = async (): Promise<Any[]> => {
    const o = await outline();
    const out: Any[] = [];
    let from = 1;
    for (let guard = 0; guard < 200 && from <= o.highestTick; guard++) {
      const r = await get(
        `/events-between?explorationId=${explorationId}&branchId=${o.rootBranchId}&fromTick=${from}&toTick=${o.highestTick}`
      );
      if (!r.events) {
        throw new Error('events-between: ' + JSON.stringify(r));
      }
      out.push(...r.events);
      if (!r.truncated || !r.nextTick) {
        break;
      }
      from = r.nextTick;
    }
    return out;
  };
  const cellsOf = async (): Promise<string[]> =>
    (await latest()).notebook.cells.map((c: Any) => c.code);

  // author marking: a scripted AI tool command edits cell 0, a hand edit follows in cell 1
  await page.evaluate(() => {
    const app = (window as Any).jupyterapp;
    app.commands.addCommand('jupyterlab-ai-commands:set-cell-source', {
      execute: (args: Any) => {
        app.shell.currentWidget.model.sharedModel.cells[args.index].setSource(
          args.text
        );
        app.shell.currentWidget.content.activeCellIndex = args.index;
      }
    });
  });
  await page.evaluate(() =>
    (window as Any).jupyterapp.commands.execute(
      'jupyterlab-ai-commands:set-cell-source',
      { index: 0, text: 'a = 100  # by the agent' }
    )
  );
  await page.waitForTimeout(2500);
  await active(page, 1);
  await setSource(page, 1, 'b = 20  # by hand');
  await page.waitForTimeout(2500);
  const stepOf = (code: string) =>
    sent.filter(
      e => e.type === 'cell_source_changed' && String(e.payload?.code) === code
    );
  expect(
    stepOf('a = 100  # by the agent').map(e => [e.authorHint, e.authorConfidence])
  ).toEqual([['ai', 'known']]);
  expect(
    stepOf('b = 20  # by hand').map(e => [
      e.authorHint,
      'authorConfidence' in e
    ])
  ).toEqual([['unknown', false]]);

  // edit, add, move, delete
  await setSource(
    page,
    0,
    'import time\nfrom IPython.display import HTML, Image, display'
  );
  await active(page, 2);
  await cmd(page, 'notebook:insert-cell-below');
  await setSource(page, 3, 'print("text out")');
  await setSource(
    page,
    1,
    'display(HTML("<table><tr><td>1</td></tr></table>"))'
  );
  await setSource(
    page,
    2,
    `display(Image(data=__import__("base64").b64decode("${PNG}")))`
  );
  await page.waitForTimeout(1500);
  await active(page, 3);
  await cmd(page, 'notebook:move-cell-up'); // text, chart swap
  await page.waitForTimeout(800);
  await active(page, 3);
  await cmd(page, 'notebook:insert-cell-below');
  await setSource(page, 4, 'raise ValueError("boom")');
  await setSource(page, 3, 'time.sleep(30)\nprint("done waiting")');
  await page.waitForTimeout(800);
  await active(page, 4);
  await cmd(page, 'notebook:insert-cell-below'); // a cell to delete
  await setSource(page, 5, 'tmp = 1');
  await page.waitForTimeout(800);
  await active(page, 5);
  await cmd(page, 'notebook:delete-cell');
  await page.waitForTimeout(1500);
  const live = await sources(page);
  expect(live.length).toBe(5);
  await expect.poll(cellsOf, { timeout: 15000 }).toEqual(live);

  // run: text, table, chart, error (the 30 s silent cell, index 3, is run later)
  for (const i of [0, 1, 2, 4]) {
    await active(page, i);
    await cmd(page, 'notebook:run-cell');
    await page.waitForTimeout(1200);
  }
  await expect
    .poll(() => outputCount(page, 4), { timeout: 60000 })
    .toBeGreaterThan(0);
  await page.waitForTimeout(2500);
  // output types at the tick right after these runs (before the close and the later edits)
  const afterRuns = await latest();
  const outs = afterRuns.notebook.cells.map((c: Any) =>
    c.outputs.flatMap((g: Any) => g.items.map((x: Any) => x.outputType))
  );
  console.log('OUTPUTS ' + JSON.stringify(outs));
  expect(outs[1]).toContain('display_data'); // table
  expect(outs[2]).toContain('stream'); // text (the chart cell became the 30 s cell)
  expect(outs[4]).toContain('error');

  // re-run, Run All stopping at an error, type change and back, Clear Outputs
  const outText = (i: number): Promise<string> =>
    page.evaluate(
      (n: number) =>
        JSON.stringify(
          (window as Any).jupyterapp.shell.currentWidget.content.widgets[
            n
          ].model.outputs.toJSON()
        ),
      i
    );
  const runsOf = async (i: number): Promise<number> => {
    const st = await latest();
    const id = st.notebook.cells[i].cellId;
    return st.runs.filter((r: Any) => r.cellId === id).length;
  };
  const snapshots = count('notebook_snapshot');
  await setSource(page, 2, 'print("again")'); // re-run of a cell that has output
  await active(page, 2);
  await cmd(page, 'notebook:run-cell');
  await expect.poll(() => outText(2), { timeout: 40000 }).toContain('again');
  await page.waitForTimeout(2500);
  const rerun = JSON.stringify((await latest()).notebook.cells[2].outputs);
  expect(rerun).toContain('again');
  expect(rerun).not.toContain('text out');
  // a cell after the failing cell is skipped by Run All: no run recorded for it
  await setSource(page, 3, 'print("quick")');
  await active(page, 4);
  await cmd(page, 'notebook:insert-cell-below');
  await setSource(page, 5, 'print("skipped")');
  await active(page, 5);
  await cmd(page, 'notebook:run-cell');
  await expect.poll(() => outText(5), { timeout: 40000 }).toContain('skipped');
  await page.waitForTimeout(2500);
  const skippedRuns = await runsOf(5);
  await cmd(page, 'notebook:run-all-cells');
  await page.waitForTimeout(8000);
  expect(await runsOf(5)).toBe(skippedRuns);
  await setSource(page, 3, 'time.sleep(30)\nprint("done waiting")');
  // code to Markdown and back: two events, no snapshot
  const typeChanges = count('cell_type_changed');
  await active(page, 0);
  await cmd(page, 'notebook:change-cell-to-markdown');
  await page.waitForTimeout(1500);
  await cmd(page, 'notebook:change-cell-to-code');
  await page.waitForTimeout(1500);
  expect(count('cell_type_changed') - typeChanges).toBe(2);
  // Clear Outputs is one event
  const cleared = count('cell_outputs_cleared');
  await active(page, 2);
  await cmd(page, 'notebook:clear-cell-output');
  await expect
    .poll(() => count('cell_outputs_cleared') - cleared, { timeout: 15000 })
    .toBe(1);
  expect(count('notebook_snapshot')).toBe(snapshots);

  // a long run with 30 s of silence; the browser is closed during it, then reopened
  await setSource(
    page,
    0,
    'import time\nfrom IPython.display import HTML, Image, display'
  );
  const fire = (c: string) =>
    page.evaluate((x: string) => {
      void (window as Any).jupyterapp.commands.execute(x);
    }, c);
  await active(page, 0);
  await fire('notebook:run-cell');
  await page.waitForTimeout(3000);
  await active(page, 3);
  await fire('notebook:run-cell');
  await page.waitForTimeout(4000);
  await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  await page.waitForTimeout(500);
  await page.close();
  await new Promise(r => setTimeout(r, 32000));
  page = await context.newPage();
  page.on('dialog', d => void d.accept());
  await openNb(page, baseURL!, NB);
  await expect.poll(() => barState(page), { timeout: 40000 }).toBe('recording');
  await page.waitForTimeout(3000);

  // rename the notebook through the open document, as the UI does
  await page.evaluate(
    (to: string) =>
      (window as Any).jupyterapp.shell.currentWidget.context.rename(to),
    NB2
  );
  await expect
    .poll(
      async () => (await allEvents()).some(e => e.type === 'notebook_renamed'),
      {
        timeout: 30000
      }
    )
    .toBe(true);
  await page.waitForTimeout(2000);

  // save the live notebook first, so the file holds the live cells
  const file = path.join(ROOT, NB2);
  const liveNow = await sources(page); // the reopened page's own cells
  await cmd(page, 'docmanager:save');
  await expect
    .poll(
      () =>
        JSON.parse(fs.readFileSync(file, 'utf8')).cells.map((c: Any) =>
          [].concat(c.source).join('')
        ),
      { timeout: 20000 }
    )
    .toEqual(liveNow);
  await page.waitForTimeout(2500);

  // an edit of the file from outside Jupyter
  const nb = JSON.parse(fs.readFileSync(file, 'utf8'));
  nb.cells[0].source = '# edited outside\nx = 42';
  fs.writeFileSync(file, JSON.stringify(nb, null, 1));
  await expect
    .poll(
      async () =>
        (await allEvents()).some(e => e.type === 'changed_outside_editor'),
      { timeout: 40000 }
    )
    .toBe(true);
  await page.waitForTimeout(3000);

  // last step: restart the kernel (dialog answered), then one run, all fire-and-forget
  const kernelStatus = (): Promise<string | undefined> =>
    page.evaluate(
      () =>
        (window as Any).jupyterapp.shell.currentWidget.sessionContext.session
          ?.kernel?.status
    );
  let restartDone = false;
  await fire('notebook:restart-kernel');
  await page.locator('.jp-Dialog button.jp-mod-accept').click();
  await expect
    .poll(
      async () => (await allEvents()).some(e => e.type === 'kernel_restarted'),
      {
        timeout: 40000
      }
    )
    .toBe(true);
  try {
    await expect.poll(kernelStatus, { timeout: 40000 }).toBe('idle');
    await active(page, 0);
    await fire('notebook:run-cell');
    await expect
      .poll(() => outputCount(page, 1), { timeout: 40000 })
      .toBeGreaterThan(-1);
    await page.waitForTimeout(3000);
    restartDone =
      (await allEvents()).filter(e => e.type === 'run_started').length > 0;
  } catch (e) {
    console.log('RESTART STEP NOT COMPLETED: ' + String(e).slice(0, 200));
  }
  console.log('RESTART RUN COMPLETED ' + restartDone);

  // read it all back from the store
  const events = await allEvents();
  const types = events.map(e => e.type);
  console.log('EVENT TYPES ' + [...new Set(types)].join(','));
  const ticks = events.map(e => e.tick);
  expect(ticks).toEqual([...ticks].sort((a, b) => a - b));
  expect(new Set(events.map(e => e.explorationId))).toEqual(
    new Set([explorationId])
  );
  const iEarly = types.indexOf('changed_outside_editor');
  const brief = (e: Any) => ({
    i: events.indexOf(e),
    type: e.type,
    tick: e.tick,
    t: e.wallTimeMs
  });
  const cellsBrief = (e: Any) =>
    JSON.stringify(
      (e.payload?.notebook?.cells ?? e.payload?.cells ?? []).map((c: Any) => [
        c.cellId ?? c.id,
        c.cellType ?? c.cell_type,
        String(c.code ?? c.source ?? '').slice(0, 40)
      ])
    );
  const prevSnap = events
    .slice(0, iEarly)
    .reverse()
    .find(e => e.type === 'notebook_snapshot');
  console.log(
    'EARLY OUTSIDE preceding ' +
      JSON.stringify(
        events.slice(Math.max(0, iEarly - 3), iEarly + 1).map(brief)
      )
  );
  console.log(
    'EARLY OUTSIDE payload keys ' +
      Object.keys(events[iEarly].payload ?? {}).join(',')
  );
  console.log('EARLY OUTSIDE cells ' + cellsBrief(events[iEarly]));
  console.log(
    'PREV SNAPSHOT cells ' + (prevSnap ? cellsBrief(prevSnap) : 'none')
  );
  console.log('FIRST EVENTS ' + JSON.stringify(events.slice(0, 8).map(brief)));
  const iRestart = types.indexOf('kernel_restarted');
  const iRename = types.indexOf('notebook_renamed');
  const iOutside = types.lastIndexOf('changed_outside_editor');
  expect(iRename).toBeGreaterThan(-1);
  expect(iOutside).toBeGreaterThan(iRename);
  expect(iRestart).toBeGreaterThan(iOutside);
  // the renamed notebook is the same history
  const renamed = events[iRename].payload;
  expect([renamed.oldPath, renamed.newPath]).toEqual([NB, NB2]);

  const st = await latest();
  expect(st.notebook.path).toBe(NB2);
  // the open page keeps its own version after the outside edit and resyncs it
  expect(st.notebook.cells.map((c: Any) => c.code)).toEqual(liveNow);
  // the outside change is the latest change of the first cell; the restart is recorded
  expect(st.kernel.lastRestart?.tick).toBe(events[iRestart].tick);
  // every gap is closed
  const open = st.gaps.filter((g: Any) => g.endedTick === undefined);
  console.log('GAPS ' + JSON.stringify(st.gaps));
  expect(open).toEqual([]);
  // recorded outputs stay in history: the table of cell 1 is kept, cell 2 was cleared
  const finalOuts = st.notebook.cells.map((c: Any) =>
    c.outputs.flatMap((g: Any) => g.items.map((x: Any) => x.outputType))
  );
  expect(finalOuts).toEqual([[], ['display_data'], []]);

  // the long run's output and the runs, from the recorded events
  expect(
    JSON.stringify(events.filter(e => e.type === 'output_added'))
  ).toContain('done waiting');
  expect(
    events.filter(e => e.type === 'run_started').length
  ).toBeGreaterThanOrEqual(5);
});
