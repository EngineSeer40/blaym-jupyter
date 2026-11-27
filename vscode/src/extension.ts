import * as path from 'path';
import * as vscode from 'vscode';
import { startService, type RunningService } from './service';
import { ServiceTransport } from './transport';
import { isRecording, setNotebookRecording, watchNotebooks } from './notebook';
import { blaym } from './state';

let service: RunningService | undefined;

export async function activate(
  context: vscode.ExtensionContext
): Promise<void> {
  const item = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left
  );
  item.command = 'blaym.toggleRecording';
  context.subscriptions.push(item);
  const active = () => vscode.window.activeNotebookEditor?.notebook.uri;
  const refresh = (notRecording?: string) => {
    if (notRecording !== undefined) {
      item.text = notRecording;
    } else {
      const uri = active();
      item.text =
        uri && isRecording(uri) ? 'Blaym: recording' : 'Blaym: not recording';
    }
    item.show();
  };
  context.subscriptions.push(
    vscode.commands.registerCommand('blaym.toggleRecording', () => {
      const doc = vscode.window.activeNotebookEditor?.notebook;
      if (!doc) {
        return;
      }
      if (doc.uri.scheme !== 'file') {
        refresh('Blaym: untitled notebooks are not recorded');
        return;
      }
      void setNotebookRecording(doc, !isRecording(doc.uri));
    }),
    vscode.window.onDidChangeActiveNotebookEditor(() => refresh())
  );

  const configured = vscode.workspace
    .getConfiguration('blaym')
    .get<string>('servicePython');
  const python = configured
    ? configured
    : path.join(context.extensionPath, '..', '.venv', 'bin', 'python');
  const started = await startService({
    python,
    // BLAYM_STORE_ROOT lets the end-to-end run use a temporary store
    root:
      process.env.BLAYM_STORE_ROOT ??
      path.join(context.globalStorageUri.fsPath, 'blaym')
  });
  if (!started.ok) {
    refresh(
      started.storeInUse
        ? 'Blaym: recording in another VS Code window'
        : `Blaym is not recording: ${started.reason}`
    );
    return;
  }
  service = started;
  refresh();
  blaym.transport = new ServiceTransport(started.port, started.token);
  blaym.isRecording = isRecording;
  context.subscriptions.push(watchNotebooks(vscode, refresh, () => refresh()));
}

export async function deactivate(): Promise<void> {
  const closers = blaym.closers;
  for (const kind of ['tracker', 'session'] as const) {
    for (const c of closers[kind]) {
      try {
        await c.close();
      } catch {
        // closing must reach the service stop below
      }
    }
    closers[kind].clear();
  }
  await service?.stop();
  service = undefined;
}
