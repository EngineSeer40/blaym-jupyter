// Task 19: author marking in a real JupyterLab page (S5-L). Jupyter AI is not installed here, so the
// scripted AI turn registers stand-ins for its tool commands (ids `jupyterlab-ai-commands:*`, as the
// S2 fake agent's tool calls reach the page) and an inline suggestion is a ghost-text element naming
// its provider plus the real `inline-completer:accept` command.
import { expect, test } from '@jupyterlab/galata';
import * as fs from 'fs';
import * as path from 'path';

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;
const ROOT = process.env.BLAYM_ROOT as string;
const NB = 'ai19.ipynb';

test.use({
  autoGoto: false,
  waitForApplication: async ({}, use) => {
    await use(async () => undefined);
  }
});

test('AI edits are marked, hand edits are not', async ({
  page: galata,
  baseURL
}) => {
  test.setTimeout(120000);
  const page = galata.page;
  const nb = {
    cells: ['a = 1', 'b = 2', 'c = 3', 'd = 4'].map((src, i) => ({
      cell_type: 'code',
      id: `ai-cell-${i}`,
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
  fs.writeFileSync(path.join(ROOT, NB), JSON.stringify(nb, null, 1));
  const sent: Any[] = [];
  page.on('request', r => {
    if (r.url().includes('/blaym-jupyter/api/ingest')) {
      sent.push(...(r.postDataJSON()?.events ?? []));
    }
  });
  await page.goto(`${baseURL}/lab/tree/${NB}`);
  await page.waitForFunction(
    (r: string) =>
      (window as Any).jupyterapp?.shell?.currentWidget?.context?.path === r &&
      !!(window as Any).jupyterapp.shell.currentWidget.model?.cells?.length,
    NB,
    { timeout: 40000 }
  );
  await expect
    .poll(
      () =>
        page.locator('.blaym-toggle').first().getAttribute('data-blaym-state'),
      { timeout: 20000 }
    )
    .toBe('off');
  await page.locator('.blaym-toggle').first().click();
  await expect
    .poll(
      () =>
        page.locator('.blaym-toggle').first().getAttribute('data-blaym-state'),
      { timeout: 20000 }
    )
    .toBe('recording');
  await page.waitForTimeout(2000);

  // the stand-in AI tool command and a record of what commandExecuted reports
  await page.evaluate(() => {
    const app = (window as Any).jupyterapp;
    const seen: string[] = [];
    (window as Any).__cmds = seen;
    app.commands.commandExecuted.connect((_: Any, a: Any) => seen.push(a.id));
    app.commands.addCommand('jupyterlab-ai-commands:set-cell-source', {
      execute: (args: Any) => {
        const w = app.shell.currentWidget;
        w.model.sharedModel.cells[args.index].setSource(args.text);
        w.content.activeCellIndex = args.index;
      }
    });
  });
  const setSource = (index: number, text: string) =>
    page.evaluate(
      ([i, t]: [number, string]) => {
        const w = (window as Any).jupyterapp.shell.currentWidget;
        w.content.activeCellIndex = i;
        w.model.sharedModel.cells[i].setSource(t);
      },
      [index, text] as [number, string]
    );
  const ghost = (provider: string) =>
    page.evaluate((p: string) => {
      document.querySelectorAll('.fake-ghost').forEach(e => e.remove());
      const g = document.createElement('span');
      g.className = 'fake-ghost';
      g.setAttribute('data-provided-by', p);
      document.body.appendChild(g);
    }, provider);
  const accept = () =>
    page.evaluate(() =>
      (window as Any).jupyterapp.commands
        .execute('inline-completer:accept')
        .catch(() => undefined)
    );
  const steps = (code: string) =>
    sent.filter(
      e => e.type === 'cell_source_changed' && String(e.payload?.code) === code
    );

  // 1. scripted AI turn: the tool command edits cell 0
  await page.evaluate(() =>
    (window as Any).jupyterapp.commands.execute(
      'jupyterlab-ai-commands:set-cell-source',
      { index: 0, text: 'a = 100  # by the agent' }
    )
  );
  await page.waitForTimeout(2500);
  // 2. a hand edit 1 s later, in another cell
  await page.waitForTimeout(1000);
  await setSource(1, 'b = 20  # by hand');
  await page.waitForTimeout(2500);
  // 3. inline completion from an AI provider, accepted
  await ghost('jupyter-ai-fake');
  await setSource(2, 'c = 30  # inline from ai');
  await accept();
  await page.waitForTimeout(2500);
  // 4. inline completion from a provider that is not AI, accepted
  await ghost('history-completer');
  await setSource(3, 'd = 40  # inline from history');
  await accept();
  await page.waitForTimeout(2500);

  const cmds: string[] = await page.evaluate(() => (window as Any).__cmds);
  const out = {
    commandsSeen: cmds.filter(c => /ai-commands|inline-completer/.test(c)),
    agent: steps('a = 100  # by the agent').map(e => [
      e.authorHint,
      e.authorConfidence
    ]),
    hand: steps('b = 20  # by hand').map(e => [
      e.authorHint,
      e.authorConfidence,
      'authorConfidence' in e
    ]),
    aiInline: steps('c = 30  # inline from ai').map(e => [
      e.authorHint,
      e.authorConfidence
    ]),
    otherInline: steps('d = 40  # inline from history').map(e => [
      e.authorHint,
      'authorConfidence' in e
    ])
  };
  console.log('AI-MARKING', JSON.stringify(out));
  expect(out.agent).toEqual([['ai', 'known']]);
  expect(out.hand).toEqual([['unknown', undefined, false]]);
  expect(out.aiInline).toEqual([['ai', 'known']]);
  expect(out.otherInline).toEqual([['unknown', false]]);
});
