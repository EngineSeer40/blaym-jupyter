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
      id: `cell-${i}`,
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
  await active(page, 0);
  await cmd(page, 'notebook:change-cell-to-markdown');
  await page.waitForTimeout(1500);
  await stateMatches('type changed');
  await cmd(page, 'notebook:change-cell-to-code');
  await page.waitForTimeout(1500);
  await stateMatches('type changed back');

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

  // a second cell with output, then Clear Outputs on the first only (a user clearing outputs
  // is not seen through the shared cell's `changed`; the output-area model finds it)
  await page.evaluate(() => {
    (
      window as Any
    ).jupyterapp.shell.currentWidget.model.sharedModel.cells[1].setSource(
      "print('second')"
    );
  });
  await active(page, 1);
  await cmd(page, 'notebook:run-cell');
  const outCount = (i: number) =>
    page.evaluate(
      (n: number) =>
        (window as Any).jupyterapp.shell.currentWidget.content.widgets[n].model
          .outputs.length,
      i
    );
  await expect.poll(() => outCount(1), { timeout: 60000 }).toBeGreaterThan(0);
  await page.waitForTimeout(2500);
  await stateMatches('second cell run');
  await active(page, 0);
  await cmd(page, 'notebook:clear-cell-output');
  expect(await outCount(0)).toBe(0);
  const outputsInState = async (): Promise<number[]> => {
    const oc = await (
      await request.get(`${API}/outline?explorationId=${explorationId}`)
    ).json();
    const sc = await (
      await request.get(
        `${API}/state?explorationId=${explorationId}&branchId=${oc.rootBranchId}&tick=${oc.highestTick}`
      )
    ).json();
    return sc.state.notebook.cells.map((c: Any) => c.outputs.length);
  };
  await expect
    .poll(async () => (await outputsInState()).slice(0, 2).map(n => n > 0), {
      timeout: 20000,
      intervals: [500]
    })
    .toEqual([false, true]);
  await stateMatches('outputs cleared');

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
