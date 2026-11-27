// The slice's one slow end-to-end test (run only through test/run.sh).
// Phase 1 drives a scripted session in real VS Code and records what VS Code shows after each step;
// the run then kills VS Code. Phase 2 reopens the notebook, checks the start-up gap and that recording
// resumes by itself, and compares every checkpoint with the state the service rebuilds at its tick.
import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import type { RebuiltState } from 'blaym-notebook-types';
import { blaym } from '../../src/state';
import { ServiceTransport } from '../../src/transport';
import { isRecording } from '../../src/notebook';

const dir = process.env.BLAYM_E2E_DIR!;
const s3c = process.env.S3C_ROOT!;
const phase = process.env.BLAYM_E2E_PHASE ?? '1';
/** Export mode (BLAYM_EXPORT=1, phase 1 only): the steps the JupyterLab session also ran, then the stored events go to BLAYM_EXPORT_OUT. */
const exporting = process.env.BLAYM_EXPORT === '1';
const notebookPath = path.join(dir, 'workspace', 'e2e.ipynb');
const renamedPath = path.join(dir, 'workspace', 'e2e_renamed.ipynb');
const stateFile = path.join(dir, 'checkpoints.json');

const sleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms));
const log = (m: string): void => console.log(`[e2e] ${m}`);

async function waitUntil(what: string, fn: () => boolean | Promise<boolean>, ms: number): Promise<void> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await fn()) return;
    await sleep(100);
  }
  throw new Error(`FAILED timeout waiting for ${what}`);
}
function withTimeout<T>(what: string, p: Thenable<T>, ms: number): Promise<T> {
  return Promise.race([Promise.resolve(p), sleep(ms).then(() => Promise.reject(new Error(`FAILED STEP ${what}: exceeded ${ms / 1000} s`)))]);
}

// ---- the notebook the session starts from -------------------------------------------------------
const code = (id: string, source: string) => ({
  cell_type: 'code',
  id,
  execution_count: null,
  metadata: {},
  outputs: [],
  source: source.split('\n').map((l, i, a) => (i < a.length - 1 ? l + '\n' : l))
});
const PNG = [
  'import base64, zlib, struct',
  'from IPython.display import display',
  'def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)',
  'raw = b"".join(b"\\x00" + bytes([255, 0, 0]) * 8 for _ in range(8))',
  'png = b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")',
  'display({"image/png": base64.b64encode(png).decode(), "text/plain": "<Figure>"}, raw=True)'
].join('\n');
const ALL_CELLS = [
  { cell_type: 'markdown', id: 'e2e-md', metadata: {}, source: ['# End to end'] },
  code('e2e-x', 'x = 1'),
  code('e2e-text', "print('hello')\n42"),
  code('e2e-table', "from IPython.display import display\ndisplay({'text/html': '<table><tr><th>a</th></tr><tr><td>1</td></tr></table>', 'text/plain': '   a\\n0  1'}, raw=True)"),
  code('e2e-chart', PNG),
  code('e2e-error', "raise ValueError('e2e error')"),
  code('e2e-slow', "import time\nprint('slow start', flush=True)\ntime.sleep(4)\nprint('slow end')")
];
const CELLS = exporting ? ALL_CELLS.filter(c => c.id !== 'e2e-slow') : ALL_CELLS;
const initialNotebook = (): string =>
  JSON.stringify({ cells: CELLS, metadata: { kernelspec: { display_name: 'Python 3', language: 'python', name: 'python3' }, language_info: { name: 'python' } }, nbformat: 4, nbformat_minor: 5 }, null, 1);

// ---- what VS Code shows, and what the rebuilt state says, in one shape ---------------------------
interface ShownCell {
  id?: string;
  type: string;
  code: string;
  out: string[];
  run: 'ok' | 'error' | 'running' | 'unknown' | 'none';
}
interface Checkpoint {
  name: string;
  tick: number;
  cells: ShownCell[];
  path?: string;
  /** compare code and ids only (the editor holds text the file does not) */
  codeOnly?: boolean;
  /** a reloaded document has no run information to compare */
  skipRuns?: boolean;
  /** cells whose outputs are not compared: VS Code's crash text on a cell is not recorded, by the owner's rule */
  skipOutputsFor?: string[];
}

function outputSignature(o: vscode.NotebookCellOutput): string {
  const mimes = o.items.map(i => i.mime);
  if (mimes.some(m => m.endsWith('.stdout') || m.endsWith('.stderr'))) return 'stream';
  if (mimes.includes('application/vnd.code.notebook.error')) return 'error';
  return [...mimes].sort().join('+');
}
function shownCells(doc: vscode.NotebookDocument): ShownCell[] {
  return doc.getCells().map(c => {
    const s = c.executionSummary;
    let run: ShownCell['run'] = 'none';
    if (s?.executionOrder !== undefined || s?.timing) {
      run = s.success === true ? 'ok' : s.success === false ? 'error' : s.timing && s.timing.endTime === undefined ? 'running' : 'unknown';
    }
    return {
      id: typeof c.metadata.id === 'string' ? c.metadata.id : undefined,
      type: c.kind === vscode.NotebookCellKind.Code ? 'code' : c.document.languageId === 'raw' ? 'raw' : 'markdown',
      code: c.document.getText(),
      out: c.outputs.map(outputSignature),
      run
    };
  });
}
function rebuiltCells(state: RebuiltState): ShownCell[] {
  return state.notebook.cells.map(c => {
    const runs = state.runs.filter(r => r.cellId === c.cellId).sort((a, b) => a.startedTick - b.startedTick);
    const last = runs[runs.length - 1]?.status;
    return {
      id: c.cellId,
      type: c.cellType,
      code: c.code,
      out: c.outputs.flatMap(g =>
        g.items.map(i => (i.outputType === 'stream' ? 'stream' : i.outputType === 'error' ? 'error' : [...i.mimeTypes].sort().join('+')))
      ),
      run: last === 'ok' || last === 'error' || last === 'running' ? last : last ? 'unknown' : 'none'
    };
  });
}
function differences(want: Checkpoint, got: RebuiltState): string[] {
  const diffs: string[] = [];
  const have = rebuiltCells(got);
  if (want.path !== undefined && got.notebook.path !== want.path) diffs.push(`path ${got.notebook.path} != ${want.path}`);
  if (have.length !== want.cells.length) diffs.push(`${have.length} cells rebuilt, VS Code showed ${want.cells.length}`);
  want.cells.forEach((w, i) => {
    const h = have[i];
    if (!h) return;
    if (w.id !== undefined && h.id !== w.id) diffs.push(`cell ${i}: id ${h.id} != ${w.id}`);
    if (h.type !== w.type) diffs.push(`cell ${i}: type ${h.type} != ${w.type}`);
    if (h.code !== w.code) diffs.push(`cell ${i}: code ${JSON.stringify(h.code)} != ${JSON.stringify(w.code)}`);
    if (want.codeOnly) return;
    if (!want.skipOutputsFor?.includes(w.id ?? '') && JSON.stringify(h.out) !== JSON.stringify(w.out)) diffs.push(`cell ${i}: outputs ${h.out.join(',')} != ${w.out.join(',')}`);
    if (!want.skipRuns && w.run !== 'unknown' && h.run !== w.run && !(w.run === 'none' && h.run === 'unknown')) diffs.push(`cell ${i}: run ${h.run} != ${w.run}`);
  });
  return diffs;
}

// ---- the service, as the extension signed in to it -----------------------------------------------
function service(): { port: number; token: string } {
  const t = blaym.transport as unknown as { port: number; token: string } | undefined;
  if (!t) throw new Error('FAILED the extension has no service');
  return t;
}
async function get<T>(route: string, query: Record<string, string | number>): Promise<T | undefined> {
  const { port, token } = service();
  const q = new URLSearchParams(Object.entries(query).map(([k, v]): [string, string] => [k, String(v)])).toString();
  const r = await fetch(`http://127.0.0.1:${port}${route}?${q}`, { headers: { Authorization: `Bearer ${token}` } });
  return r.ok ? ((await r.json()) as T) : undefined;
}
interface Outline {
  rootBranchId: string;
  highestTick: number;
}
let explorationId: string | undefined;
const outline = (): Promise<Outline | undefined> => get<Outline>('/outline', { explorationId: explorationId! });
const stateAt = async (tick: number): Promise<{ status: string; state: RebuiltState }> =>
  (await get('/state', { explorationId: explorationId!, branchId: (await outline())!.rootBranchId, tick }))!;

/** Wait until the history has stopped growing for a while, then return its newest tick. */
async function settle(minQuietMs = 1800): Promise<number> {
  let last = -1;
  let since = Date.now();
  const end = Date.now() + 40000;
  while (Date.now() < end) {
    const t = (await outline())?.highestTick ?? 0;
    if (t !== last) {
      last = t;
      since = Date.now();
    } else if (Date.now() - since >= minQuietMs) return t;
    await sleep(200);
  }
  throw new Error('FAILED the history never went quiet');
}

function captureExplorationId(): void {
  const proto = ServiceTransport.prototype;
  const original = proto.register;
  proto.register = async function (this: ServiceTransport, req) {
    const status = await original.call(this, req);
    explorationId = status.explorationId;
    return status;
  };
}

async function activate(): Promise<void> {
  await vscode.extensions.getExtension('blaym.blaym-vscode')!.activate();
  await waitUntil('service', () => blaym.transport !== undefined, 30000);
}

const findDoc = (p: string): vscode.NotebookDocument | undefined => vscode.workspace.notebookDocuments.find(n => n.uri.fsPath === p);

// ---- kernel selection, as in the S3c probe ---------------------------------------------------------
async function selectVenvKernel(): Promise<void> {
  const kernelPython = path.join(s3c, '.venv-kernel', 'bin', 'python');
  const jup = vscode.extensions.getExtension('ms-toolsai.jupyter')!;
  const py = vscode.extensions.getExtension('ms-python.python')!;
  const pyApi = await withTimeout('activate python', py.activate(), 60000);
  const jupApi = await withTimeout('activate jupyter', jup.activate(), 60000);
  const jr = (jupApi as { ready?: Thenable<void> } | undefined)?.ready;
  if (jr) await withTimeout('jupyter ready', jr, 60000);
  await withTimeout('set interpreter', pyApi.environments.updateActiveEnvironmentPath(kernelPython, vscode.workspace.workspaceFolders?.[0]), 30000);
  const exeRe = kernelPython.replace('/bin/python', '/python').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const idRe = new RegExp(`ms-toolsai\\.jupyter/(\\.jvsc74a57bd0[0-9a-f]+\\.${exeRe}\\.[^,\\s]*)`);
  const findId = (): string | undefined => {
    const stack = [path.join(s3c, 'vscode', 'portable', 'user-data', 'logs')];
    let found: string | undefined;
    while (stack.length) {
      const d = stack.pop()!;
      for (const e of fs.readdirSync(d, { withFileTypes: true })) {
        const f = path.join(d, e.name);
        if (e.isDirectory()) stack.push(f);
        else if (e.name === 'renderer.log') {
          const m = fs.readFileSync(f, 'utf8').match(idRe);
          if (m) found = m[1];
        }
      }
    }
    return found;
  };
  let id: string | undefined;
  const end = Date.now() + 60000;
  while (!id && Date.now() < end) {
    await withTimeout('bogus selectKernel', vscode.commands.executeCommand('notebook.selectKernel', { id: 'probe-bogus', extension: 'ms-toolsai.jupyter' }), 10000).then(undefined, () => undefined);
    await sleep(1500);
    id = findId();
  }
  if (!id) throw new Error('FAILED STEP discover-kernel-id');
  await withTimeout('selectKernel', vscode.commands.executeCommand('notebook.selectKernel', { id, extension: 'ms-toolsai.jupyter' }), 30000);
}

function kernelPids(): number[] {
  const venv = path.join(s3c, '.venv-kernel');
  const ps = cp.spawnSync('ps', ['-eo', 'pid=,args='], { encoding: 'utf8' }).stdout;
  return ps
    .split('\n')
    .map(l => l.trim().match(/^(\d+)\s+(.*)$/))
    .filter((m): m is RegExpMatchArray => !!m && m[2].includes(venv) && m[2].includes('ipykernel_launcher'))
    .map(m => Number(m[1]));
}

// ---- phase 1: the scripted session ---------------------------------------------------------------
async function scriptedSession(): Promise<void> {
  fs.writeFileSync(notebookPath, initialNotebook());
  captureExplorationId();
  const uri = vscode.Uri.file(notebookPath);
  let doc = await vscode.workspace.openNotebookDocument(uri);
  await vscode.window.showNotebookDocument(doc);
  await activate();
  await waitUntil('exploration id', () => explorationId !== undefined, 30000);
  await selectVenvKernel();
  await vscode.commands.executeCommand('blaym.toggleRecording');
  await waitUntil('recording', () => isRecording(doc.uri), 30000);
  const checkpoints: Checkpoint[] = [];
  const save = () => fs.writeFileSync(stateFile, JSON.stringify({ explorationId, checkpoints, renamedPath }, null, 1));
  const checkpoint = async (name: string, extra: Partial<Checkpoint> = {}, d: vscode.NotebookDocument = doc) => {
    const tick = await settle();
    checkpoints.push({ name, tick, cells: shownCells(d), ...extra });
    save();
    log(`checkpoint ${name} at tick ${tick}`);
  };
  const select = async (i: number) => {
    const editor = await vscode.window.showNotebookDocument(doc, { selections: [new vscode.NotebookRange(i, i + 1)] });
    editor.selection = new vscode.NotebookRange(i, i + 1);
    await sleep(200);
  };
  const edit = async (...edits: vscode.NotebookEdit[]) => {
    const we = new vscode.WorkspaceEdit();
    we.set(doc.uri, edits);
    await vscode.workspace.applyEdit(we);
  };
  const type = async (i: number, text: string, at: 'start' | 'end' = 'end') => {
    const d = doc.cellAt(i).document;
    const we = new vscode.WorkspaceEdit();
    we.insert(d.uri, at === 'end' ? d.positionAt(d.getText().length) : new vscode.Position(0, 0), text);
    await vscode.workspace.applyEdit(we);
  };
  /** Starts the cell; the returned promise settles when the run is over. */
  const start = (i: number): Promise<unknown> =>
    Promise.resolve(vscode.commands.executeCommand('notebook.cell.execute', { ranges: [{ start: i, end: i + 1 }], document: doc.uri }));
  const run = (i: number): Promise<unknown> => withTimeout(`run cell ${i}`, start(i), 90000);

  await checkpoint('opened');
  await type(1, '  # edited');
  await checkpoint('edited');
  await edit(vscode.NotebookEdit.insertCells(doc.cellCount, [new vscode.NotebookCellData(vscode.NotebookCellKind.Code, 'y = 2', 'python')]));
  await checkpoint('added');
  await select(doc.cellCount - 1);
  await vscode.commands.executeCommand('notebook.cell.moveUp');
  await checkpoint('moved');
  await edit(vscode.NotebookEdit.deleteCells(new vscode.NotebookRange(doc.cellCount - 2, doc.cellCount - 1)));
  await checkpoint('deleted');
  for (const [i, name] of [[2, 'run-text'], [3, 'run-table'], [4, 'run-chart'], [5, 'run-error']] as const) {
    await run(i);
    await checkpoint(name);
  }
  if (!exporting) {
    const slow = start(6);
    await sleep(1500);
    await type(6, '\n# edited while running');
    await checkpoint('edited-while-running');
    await withTimeout('slow cell', slow, 60000);
    await checkpoint('run-finished-after-edit');
  }
  await withTimeout('restart', vscode.commands.executeCommand('jupyter.restartkernel'), 60000);
  await checkpoint('restarted');
  await run(2);
  await checkpoint('run-after-restart');
  if (!exporting) {
    const pids = kernelPids();
    if (pids.length === 0) throw new Error('FAILED no kernel process to crash');
    pids.forEach(p => process.kill(p, 'SIGKILL'));
    await sleep(6000);
    await checkpoint('crashed', { skipOutputsFor: ['e2e-text'] });
    await withTimeout('restart after crash', vscode.commands.executeCommand('jupyter.restartkernel'), 60000).then(undefined, e => log(`restart after crash: ${e}`));
    await checkpoint('restarted-after-crash', { skipOutputsFor: ['e2e-text'] });
  }

  // rename
  const we = new vscode.WorkspaceEdit();
  we.renameFile(uri, vscode.Uri.file(renamedPath));
  await vscode.workspace.applyEdit(we);
  await waitUntil('renamed document', () => findDoc(renamedPath) !== undefined, 15000);
  doc = findDoc(renamedPath)!;
  await vscode.window.showNotebookDocument(doc);
  await checkpoint('renamed', { path: renamedPath, skipRuns: true, skipOutputsFor: ['e2e-text'] });

  // outside writes, editor clean then dirty
  if (doc.isDirty) await doc.save();
  let seenTick = await settle();
  const nb = JSON.parse(fs.readFileSync(renamedPath, 'utf8'));
  nb.cells[1].source = ['x = 100  # changed outside VS Code'];
  nb.cells.push(code('e2e-outside', 'print("added from outside")'));
  fs.writeFileSync(renamedPath, JSON.stringify(nb, null, 1));
  await waitUntil('document picks up the outside change', () => doc.cellCount === nb.cells.length, 20000);
  // the editor's own save must not be recorded as an outside change: wait for the event that holds the outside cell
  await waitUntil(
    'the outside cell in the history',
    async () => {
      const o = (await outline())!;
      const r = await get<{ events: Array<{ type: string; payload: { cells?: Array<{ cellId: string }> } }> }>('/events-between', { explorationId: explorationId!, branchId: o.rootBranchId, fromTick: 1, toTick: o.highestTick });
      return !!r?.events.some(e => e.type === 'changed_outside_editor' && e.payload.cells?.some(c => c.cellId === 'e2e-outside'));
    },
    30000
  );
  await checkpoint('outside-write-clean-editor', { codeOnly: true });
  if (exporting) {
    await settle();
    const o = (await outline())!;
    const r = await get<{ events: unknown[] }>('/events-between', { explorationId: explorationId!, branchId: o.rootBranchId, fromTick: 1, toTick: o.highestTick });
    fs.writeFileSync(process.env.BLAYM_EXPORT_OUT!, JSON.stringify(r!.events, null, 1));
    log(`EXPORTED ${r!.events.length}`);
    fs.writeFileSync(path.join(dir, 'kill-me'), 'now');
    await sleep(120000);
  }

  await type(2, '# dirty edit\n', 'start');
  seenTick = await settle();
  const nb2 = JSON.parse(fs.readFileSync(renamedPath, 'utf8'));
  nb2.cells[3].source = ['print("second outside change")'];
  fs.writeFileSync(renamedPath, JSON.stringify(nb2, null, 1));
  await waitUntil('the service to record the second outside change', async () => ((await outline())?.highestTick ?? 0) > seenTick, 30000);
  // VS Code keeps the editor's text; the service records the file's: compare against what the file holds
  const fileCells = nb2.cells.map((c: { id: string; cell_type: string; source: string[] }) => ({
    id: c.id,
    type: c.cell_type,
    code: c.source.join(''),
    out: [],
    run: 'unknown' as const
  }));
  const tick = await settle();
  checkpoints.push({ name: 'outside-write-dirty-editor (file contents)', tick, cells: fileCells, codeOnly: true });
  save();
  log(`dirty editor kept its text: ${doc.isDirty && doc.cellAt(2).document.getText().startsWith('# dirty edit')}`);

  // a last unsaved edit, then VS Code is killed
  await type(1, '  # unsaved at the kill');
  await sleep(1500);
  fs.writeFileSync(path.join(dir, 'kill-me'), 'now');
  await sleep(120000);
}

// ---- phase 2: reopen, the start-up gap, resumed recording, and the comparison -----------------------
async function reopenAndCompare(): Promise<void> {
  const saved = JSON.parse(fs.readFileSync(stateFile, 'utf8')) as { explorationId: string; checkpoints: Checkpoint[]; renamedPath: string };
  explorationId = saved.explorationId;
  captureExplorationId();
  const doc = await vscode.workspace.openNotebookDocument(vscode.Uri.file(saved.renamedPath));
  await vscode.window.showNotebookDocument(doc);
  await activate();
  await waitUntil('recording resumed by itself', () => isRecording(doc.uri), 30000);
  const before = (await outline())!.highestTick;
  const marker = `# typed after reopening ${Date.now()}`;
  const d = doc.cellAt(1).document;
  const we = new vscode.WorkspaceEdit();
  we.insert(d.uri, d.positionAt(d.getText().length), marker);
  await vscode.workspace.applyEdit(we);
  const after = await settle();
  const problems: string[] = [];
  if (after <= before) problems.push('recording did not resume: no new event after typing');
  const latest = await stateAt(after);
  if (latest.status !== 'ok') problems.push(`latest state: ${latest.status}`);
  else {
    const gaps = latest.state.gaps;
    log(`gaps after reopening: ${JSON.stringify(gaps)}`);
    if (!gaps.some(g => g.endedTick !== undefined)) problems.push('no closed start-up gap after the kill');
    if (gaps.some(g => g.endedTick === undefined)) problems.push('a gap is still open');
    if (!latest.state.notebook.cells[1].code.includes(marker)) problems.push('the typing after reopening is not in the rebuilt state');
  }
  let matched = 0;
  for (const c of saved.checkpoints) {
    const r = await stateAt(c.tick);
    const diffs = r.status === 'ok' ? differences(c, r.state) : [`rebuild answered ${r.status}`];
    if (diffs.length === 0) matched++;
    else {
      problems.push(...diffs.map(x => `MISMATCH at ${c.name} (tick ${c.tick}): ${x}`));
      const ev = await get<{ events: Array<Record<string, unknown>> }>('/events-between', {
        explorationId: explorationId!,
        branchId: (await outline())!.rootBranchId,
        fromTick: 1,
        toTick: after
      });
      log(`history: ${(ev?.events ?? []).map(e => `${e.tick}:${e.type}:${String(e.cellId ?? e.runId ?? e.reason ?? '').slice(0, 8)}`).join(' ')}`);
    }
  }
  log(`checkpoints matched ${matched} of ${saved.checkpoints.length}`);
  if (problems.length) {
    problems.forEach(p => log(p));
    throw new Error(`FAILED ${problems.length} problem(s), first: ${problems[0]}`);
  }
}

export async function run(): Promise<void> {
  const t = Date.now();
  if (phase === '1') await scriptedSession();
  else await reopenAndCompare();
  log(`phase ${phase} took ${Math.round((Date.now() - t) / 1000)} s`);
}
