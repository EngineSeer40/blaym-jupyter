import * as crypto from 'crypto';
import * as path from 'path';
import type * as vscode from 'vscode';
import type { CellType } from 'blaym-notebook-types';
import type { MetadataPolicy } from '../../core/ts/src/recording/metadata';
import policy from '../../core/ts/src/recording/metadata-policy.json';
import { RunTracker } from '../../core/ts/src/recording/runs';
import {
  RecorderSession,
  notebookFingerprint
} from '../../core/ts/src/recording/session';
import type {
  CellView,
  Clock,
  Ids,
  Scheduler
} from '../../core/ts/src/recording/types';
import { watchKernel } from './kernel';
import { toNbformatOutputs } from './outputs';
import { blaym } from './state';

/** A new cell gets its nbformat `id` 8 to 40 ms after the insert; wait up to this long. */
const ID_HOLD_MS = 100;

/** A cell in a structure change, as the matcher reads it. */
export interface CellRecord {
  handle: number;
  index: number;
  text: string;
  nbformatId?: string;
}

/**
 * What one structure event means (`capture.md` Q2). The same handle on both
 * sides is a move; the same index, text and nbformat id under a new handle is
 * a type change; the rest is delete plus insert.
 */
export function matchStructure(
  removed: CellRecord[],
  added: CellRecord[]
): { moves: number[]; typeChanges: Array<{ from: number; to: number }> } {
  const gone = new Set(removed.map(r => r.handle));
  const moves = added.filter(a => gone.has(a.handle)).map(a => a.handle);
  const typeChanges: Array<{ from: number; to: number }> = [];
  for (const a of added) {
    const from = removed.find(
      r =>
        !moves.includes(r.handle) &&
        a.nbformatId !== undefined &&
        r.index === a.index &&
        r.text === a.text &&
        r.nbformatId === a.nbformatId
    );
    if (from && !moves.includes(a.handle)) {
      typeChanges.push({ from: from.handle, to: a.handle });
    }
  }
  return { moves, typeChanges };
}

const clock: Clock = { nowMs: () => Date.now() };
const scheduler: Scheduler = {
  setTimeout: (fn, ms) => {
    const t = setTimeout(fn, ms);
    return () => clearTimeout(t);
  }
};
const ids: Ids = { uuid: () => crypto.randomUUID() };
const sha256Hex = async (text: string): Promise<string> =>
  crypto.createHash('sha256').update(text).digest('hex');

const NOTICE = 'Blaym: the file changed on disk while you had unsaved edits';

/** Open notebooks with a session, by uri. */
const sessions = new Map<
  string,
  { session: RecorderSession; setRecording(on: boolean): Promise<void> }
>();

/** Renamed notebooks waiting for their new document to open, by the new uri. */
const renamed = new Map<string, (doc: vscode.NotebookDocument) => void>();

/** Whether the notebook's session is recording (the registry's flag, as the session sees it). */
export const isRecording = (uri: vscode.Uri): boolean =>
  sessions.get(uri.toString())?.session.state.kind === 'recording';

/** Switch recording on or off for an open notebook that has a session. */
export async function setNotebookRecording(
  doc: vscode.NotebookDocument,
  on: boolean
): Promise<void> {
  await sessions.get(doc.uri.toString())?.setRecording(on);
}

/**
 * Every real-file notebook open now or opened later gets a session that
 * registers with the service; the registry's flag decides whether it records.
 * `changed` is called when a session's state changes; `notice` shows a message.
 */
export function watchNotebooks(
  api: typeof vscode,
  notice: (text: string) => void,
  changed: () => void
): vscode.Disposable {
  const open = (doc: vscode.NotebookDocument) => {
    const key = doc.uri.toString();
    const adopt = renamed.get(key);
    if (adopt) {
      // a renamed notebook: the new document continues the old one's session
      renamed.delete(key);
      adopt(doc);
      return;
    }
    if (
      doc.notebookType === 'jupyter-notebook' &&
      doc.uri.scheme === 'file' &&
      blaym.transport &&
      !sessions.has(key)
    ) {
      sessions.set(
        key,
        attach(
          api,
          doc,
          notice,
          changed,
          k => {
            sessions.delete(k);
            // closed before its renamed document opened: nobody is left to adopt it
            renamed.delete(k);
          },
          (from, to, adopt) => {
            const entry = sessions.get(from);
            sessions.delete(from);
            renamed.delete(from);
            if (entry) {
              sessions.set(to, entry);
              renamed.set(to, adopt);
            }
          }
        )
      );
    }
  };
  api.workspace.notebookDocuments.forEach(open);
  return api.workspace.onDidOpenNotebookDocument(open);
}

function attach(
  api: typeof vscode,
  first: vscode.NotebookDocument,
  notice: (text: string) => void,
  changed: () => void,
  onClosed: (key: string) => void,
  onRenamed: (
    from: string,
    to: string,
    adopt: (doc: vscode.NotebookDocument) => void
  ) => void
): { session: RecorderSession; setRecording(on: boolean): Promise<void> } {
  const transport = blaym.transport as NonNullable<typeof blaym.transport>;
  const handles = new WeakMap<vscode.NotebookCell, number>();
  const viewIds = new WeakMap<vscode.NotebookCell, string>();
  const waiting = new Map<vscode.NotebookCell, number>();
  let nextHandle = 0;
  let notebookId: string | undefined;
  // the document this session reads; a rename moves it to the new document (`adopt`)
  let doc = first;
  let key = doc.uri.toString();
  let pathNow = doc.uri.fsPath;
  let saving = false;
  let saveRefused = false;
  const handleOf = (c: vscode.NotebookCell): number => {
    let h = handles.get(c);
    if (h === undefined) {
      handles.set(c, (h = nextHandle++));
    }
    return h;
  };
  const nbformatId = (c: vscode.NotebookCell): string | undefined =>
    typeof c.metadata.id === 'string' ? c.metadata.id : undefined;
  const record = (c: vscode.NotebookCell, index: number): CellRecord => ({
    handle: handleOf(c),
    index,
    text: c.document.getText(),
    nbformatId: nbformatId(c)
  });
  /** The id the recorder sees; a new cell without an id yet is left out for a moment. */
  const viewId = (c: vscode.NotebookCell): string | undefined => {
    let id = viewIds.get(c) ?? nbformatId(c);
    if (id === undefined) {
      const since = waiting.get(c);
      if (since !== undefined && clock.nowMs() - since < ID_HOLD_MS) {
        return undefined;
      }
      id = `h${handleOf(c)}`;
    }
    viewIds.set(c, id);
    waiting.delete(c);
    return id;
  };
  const cellType = (c: vscode.NotebookCell): CellType =>
    c.kind === api.NotebookCellKind.Code
      ? 'code'
      : c.document.languageId === 'raw'
        ? 'raw'
        : 'markdown';
  const nbMeta = (): Record<string, unknown> => doc.metadata.metadata ?? {};
  const stable = doc.getCells().every(c => nbformatId(c) !== undefined);

  const session = new RecorderSession(
    {
      cellIdsStable: stable,
      notebookMetadata: nbMeta,
      cells: (): CellView[] =>
        doc.getCells().flatMap(c => {
          const id = viewId(c);
          return id === undefined
            ? []
            : [
                {
                  id,
                  type: cellType(c),
                  source: c.document.getText(),
                  // VS Code's own keys sit beside `metadata`; only the file's part is read
                  metadata: c.metadata.metadata ?? {},
                  outputs:
                    c.kind === api.NotebookCellKind.Code
                      ? toNbformatOutputs(c.outputs)
                      : []
                }
              ];
        })
    },
    {
      transport,
      clock,
      scheduler,
      ids,
      policy: policy as MetadataPolicy,
      editor: 'vscode',
      path: () => pathNow,
      onState: changed
    }
  );
  const kernelInfo = () => {
    const m = doc.metadata.metadata ?? {};
    return {
      kernelName: m.kernelspec?.name ?? 'unknown',
      language: m.language_info?.name ?? m.kernelspec?.language ?? 'unknown'
    };
  };
  const tracker = new RunTracker({
    kernelInfo,
    session,
    transport,
    explorationId: () => notebookId as string,
    clock,
    scheduler,
    ids
  });
  const watchDocKernel = () =>
    watchKernel(api, doc.uri, k => tracker.observeKernel(k), kernelInfo);
  let kernel = watchDocKernel();
  blaym.closers.tracker.add(tracker);
  blaym.closers.session.add(session);

  const onStructure = (e: vscode.NotebookDocumentChangeEvent) => {
    for (const ch of e.contentChanges) {
      const removed = ch.removedCells.map((c, i) =>
        record(c, ch.range.start + i)
      );
      const added = ch.addedCells.map((c, i) => record(c, ch.range.start + i));
      const { moves, typeChanges } = matchStructure(removed, added);
      for (const t of typeChanges) {
        const from = ch.removedCells.find(c => handleOf(c) === t.from);
        const to = ch.addedCells.find(c => handleOf(c) === t.to);
        const id = from && viewIds.get(from);
        if (to && id) {
          viewIds.set(to, id);
        }
      }
      let held = false;
      ch.addedCells.forEach(c => {
        const h = handleOf(c);
        const known =
          moves.includes(h) ||
          typeChanges.some(t => t.to === h) ||
          viewIds.has(c);
        if (!known && nbformatId(c) === undefined) {
          waiting.set(c, clock.nowMs());
          held = true;
        }
      });
      if (held) {
        scheduler.setTimeout(() => session.process(), ID_HOLD_MS + 5);
      }
    }
  };

  const subs: vscode.Disposable[] = [
    // typing comes from the cell's text document; the notebook duplicates carry only `document`
    api.workspace.onDidChangeTextDocument(e => {
      if (doc.getCells().some(c => c.document === e.document)) {
        session.process();
      }
    }),
    api.workspace.onDidChangeNotebookDocument(e => {
      if (e.notebook !== doc) {
        return;
      }
      onStructure(e);
      session.process();
      for (const c of e.cellChanges) {
        if (c.executionSummary === undefined && c.outputs === undefined) {
          continue;
        }
        const id = viewId(c.cell);
        if (id === undefined) {
          continue;
        }
        if (c.executionSummary?.executionOrder !== undefined) {
          kernel.refresh();
        }
        const outputs = toNbformatOutputs(c.cell.outputs);
        const summary = c.cell.executionSummary;
        tracker.observeCell({
          cellId: id,
          executionOrder: summary?.executionOrder,
          success: summary?.success,
          errorName: outputs.find(o => o.output_type === 'error')?.ename as
            string | undefined,
          outputs
        });
      }
    }),
    api.workspace.onDidRenameFiles(e => {
      for (const f of e.files) {
        if (notebookId && f.oldUri.fsPath === pathNow) {
          pathNow = f.newUri.fsPath;
          const to = f.newUri.toString();
          onRenamed(key, to, adopt);
          key = to;
          transport
            .rename({
              notebookId,
              instanceId: session.instanceId,
              oldPath: f.oldUri.fsPath,
              newPath: pathNow
            })
            .catch(() => undefined);
        }
      }
    }),
    api.workspace.onWillSaveNotebookDocument(e => {
      if (e.notebook === doc) {
        saving = true;
        session.flushTyping();
        saveNotice('started');
      }
    }),
    api.workspace.onDidSaveNotebookDocument(d => {
      if (d === doc) {
        saving = false;
        saveNotice('finished');
      }
    }),
    api.workspace.onDidCloseNotebookDocument(d => {
      if (d === doc) {
        dispose();
      }
    })
  ];
  const saveNotice = (phase: 'started' | 'finished') => {
    if (notebookId) {
      transport
        .saveNotice({
          notebookId,
          instanceId: session.instanceId,
          path: pathNow,
          phase
        })
        .catch(e => {
          if (!saveRefused) {
            saveRefused = true;
            console.warn(`Blaym: a save notice was refused: ${String(e)}`);
          }
        });
    }
  };
  // VS Code does not reload a file that changed on disk while the editor has unsaved edits
  let fileWatch: vscode.Disposable[] = [];
  const watchFile = () => {
    fileWatch.forEach(d => d.dispose());
    const watcher = api.workspace.createFileSystemWatcher(
      new api.RelativePattern(path.dirname(pathNow), path.basename(pathNow))
    );
    fileWatch = [
      watcher,
      watcher.onDidChange(() => {
        if (doc.isDirty && !saving) {
          notice(NOTICE);
        }
      })
    ];
  };
  watchFile();
  // VS Code opened the renamed file as a new document: this session reads it from now on, with its instance
  // id and registration unchanged. Its text may still change (unsaved edits); the normal change path records that.
  const adopt = (next: vscode.NotebookDocument) => {
    doc = next;
    kernel.dispose();
    kernel = watchDocKernel();
    kernel.refresh();
    watchFile();
  };
  const dispose = () => {
    subs.forEach(s => s.dispose());
    fileWatch.forEach(d => d.dispose());
    kernel.dispose();
    tracker.close().catch(() => undefined);
    session.close();
    blaym.closers.tracker.delete(tracker);
    blaym.closers.session.delete(session);
    onClosed(key);
  };

  const started = (async () => {
    try {
      await begin();
    } catch (e) {
      notice(`Blaym is not recording: ${String(e)}`);
    }
  })();
  async function begin(): Promise<void> {
    session.setFingerprint(
      await notebookFingerprint(
        doc.getCells().map(c => c.document.getText()),
        `${doc.metadata.nbformat ?? 4}.${doc.metadata.nbformat_minor ?? 0}`,
        stable ? doc.getCells().map(c => viewId(c) as string) : undefined,
        sha256Hex
      )
    );
    const status = await session.register();
    if (status) {
      notebookId = status.notebookId;
      await session.start(status.notebookId);
      kernel.refresh();
    }
  }
  return {
    session,
    setRecording: async on => {
      await started;
      await session.setRecording(on);
    }
  };
}
