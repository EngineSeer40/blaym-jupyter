// Fixture generator, not a test of the default run: the JupyterLab side of the cross-editor check.
// The same steps as vscode/test/suite/recording.ts (edit, add, move, delete; run text, table, chart,
// error; restart and run; rename; outside write with a clean editor), then the stored events of the
// notebook's history go to fixtures/recorder/cross-editor/jupyterlab.json. Run it with
//   BLAYM_EXPORT=1 BLAYM_SPEC=cross-editor jupyter/ui-tests/run-galata.sh
import { expect, test } from '@jupyterlab/galata';
import * as fs from 'fs';
import * as path from 'path';

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

const ROOT = process.env.BLAYM_ROOT as string;
const NB = 'xed.ipynb';
const NB2 = 'xed_renamed.ipynb';
const API = '/blaym-jupyter/api';
test.use({
  autoGoto: false,
  waitForApplication: async ({}, use) => {
    await use(async () => undefined);
  }
});

const code = (id: string, source: string) => ({
  cell_type: 'code',
  id,
  execution_count: null,
  metadata: {},
  outputs: [],
  source: source.split('\n').map((l, i, a) => (i < a.length - 1 ? l + '\n' : l))
});
const CHART = [
  'import base64, zlib, struct',
  'from IPython.display import display',
  'def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)',
  'raw = b"".join(b"\\x00" + bytes([255, 0, 0]) * 8 for _ in range(8))',
  'png = b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")',
  'display({"image/png": base64.b64encode(png).decode(), "text/plain": "<Figure>"}, raw=True)'
].join('\n');

function writeNb(rel: string): void {
  const nb = {
    cells: [
      { cell_type: 'markdown', id: 'e2e-md', metadata: {}, source: ['# End to end'] },
      code('e2e-x', 'x = 1'),
      code('e2e-text', "print('hello')\n42"),
      code(
        'e2e-table',
        "from IPython.display import display\ndisplay({'text/html': '<table><tr><th>a</th></tr><tr><td>1</td></tr></table>', 'text/plain': '   a\\n0  1'}, raw=True)"
      ),
      code('e2e-chart', CHART),
      code('e2e-error', "raise ValueError('e2e error')")
    ],
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

test.skip(!process.env.BLAYM_EXPORT, 'fixture generator: set BLAYM_EXPORT=1');

test('cross-editor session export', async ({ page: galata, baseURL, request }) => {
  test.setTimeout(200000);
  const page = galata.page;
  page.on('dialog', d => void d.accept());
  writeNb(NB);
  await openNb(page, baseURL!, NB);

  const registers: Any[] = [];
  page.on('response', async r => {
    if (r.url().includes(`${API}/notebooks/register`) && r.ok()) {
      registers.push(await r.json().catch(() => undefined));
    }
  });
  await expect.poll(() => barState(page), { timeout: 20000 }).toBe('off');
  await page.locator('.blaym-toggle').first().click();
  await expect.poll(() => barState(page), { timeout: 20000 }).toBe('recording');
  await expect.poll(() => registers.length, { timeout: 10000 }).toBeGreaterThan(0);
  const explorationId: string = registers[registers.length - 1].explorationId;

  const get = async (url: string): Promise<Any> =>
    (await request.get(`${API}${url}`)).json();
  const allEvents = async (): Promise<Any[]> => {
    const o = await get(`/outline?explorationId=${explorationId}`);
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
  const fire = (c: string) =>
    page.evaluate((x: string) => {
      void (window as Any).jupyterapp.commands.execute(x);
    }, c);

  // edit, add, move, delete
  await active(page, 1);
  await setSource(page, 1, 'x = 1  # edited');
  await page.waitForTimeout(2000);
  await active(page, 5);
  await cmd(page, 'notebook:insert-cell-below');
  await setSource(page, 6, 'y = 2');
  await page.waitForTimeout(2000);
  await active(page, 6);
  await cmd(page, 'notebook:move-cell-up');
  await page.waitForTimeout(2000);
  await active(page, 5); // the added cell, now second to last
  await cmd(page, 'notebook:delete-cell');
  await page.waitForTimeout(2000);
  expect((await sources(page)).length).toBe(6);

  // run text, table, chart, error
  for (const i of [2, 3, 4, 5]) {
    await active(page, i);
    await cmd(page, 'notebook:run-cell');
    await expect.poll(() => outputCount(page, i), { timeout: 40000 }).toBeGreaterThan(0);
    await page.waitForTimeout(2000);
  }

  // restart the kernel, then run the text cell
  await fire('notebook:restart-kernel');
  await page.locator('.jp-Dialog button.jp-mod-accept').click();
  await expect
    .poll(async () => (await allEvents()).some(e => e.type === 'kernel_restarted'), {
      timeout: 40000
    })
    .toBe(true);
  // the page's kernel connection stays at "starting" after a restart in headless Chromium:
  // reload the page (the server's kernel is restarted and idle) before the next run
  await cmd(page, 'docmanager:save'); // the reload would drop unsaved edits
  await page.waitForTimeout(3000);
  await openNb(page, baseURL!, NB);
  await expect.poll(() => barState(page), { timeout: 40000 }).toBe('recording');
  await page.waitForTimeout(3000);
  const runsBefore = (await allEvents()).filter(e => e.type === 'run_started').length;
  await active(page, 2);
  await fire('notebook:run-cell');
  await expect
    .poll(async () => (await allEvents()).filter(e => e.type === 'run_finished').length, {
      timeout: 60000
    })
    .toBe(runsBefore + 1);
  await page.waitForTimeout(2000);

  // rename through the open document
  await page.evaluate(
    (to: string) => (window as Any).jupyterapp.shell.currentWidget.context.rename(to),
    NB2
  );
  await expect
    .poll(async () => (await allEvents()).some(e => e.type === 'notebook_renamed'), {
      timeout: 30000
    })
    .toBe(true);
  await page.waitForTimeout(2000);

  // outside write, editor clean (saved first)
  const file = path.join(ROOT, NB2);
  const liveNow = await sources(page);
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
  const nb = JSON.parse(fs.readFileSync(file, 'utf8'));
  nb.cells[1].source = ['x = 100  # changed outside VS Code'];
  nb.cells.push(code('e2e-outside', 'print("added from outside")'));
  fs.writeFileSync(file, JSON.stringify(nb, null, 1));
  await expect
    .poll(async () => (await allEvents()).some(e => e.type === 'changed_outside_editor'), {
      timeout: 40000
    })
    .toBe(true);
  const shown = await sources(page);
  console.log(`PAGE AFTER OUTSIDE WRITE cells=${shown.length} cell0=${JSON.stringify(shown[0])} cell1=${JSON.stringify(shown[1])}`);
  await page.waitForTimeout(3000);

  const events = await allEvents();
  const out = path.resolve(__dirname, '../../../fixtures/recorder/cross-editor/jupyterlab.json');
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(events, null, 1));
  const kinds: Record<string, number> = {};
  events.forEach(e => (kinds[e.type] = (kinds[e.type] ?? 0) + 1));
  console.log(`EXPORTED ${events.length} ${JSON.stringify(kinds)}`);
  expect(events.length).toBeGreaterThan(0);
});
