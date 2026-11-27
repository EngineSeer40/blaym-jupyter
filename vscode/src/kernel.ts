import type * as vscode from 'vscode';
import type { KernelObservation } from '../../core/ts/src/recording/runs';

const JUPYTER_EXTENSION = 'ms-toolsai.jupyter';

/** The part of the Jupyter extension's kernel API that Blaym reads. */
interface KernelLike {
  status: string;
  language?: string;
  onDidChangeStatus(listener: (status: string) => void): vscode.Disposable;
}
interface JupyterApi {
  kernels: { getKernel(uri: vscode.Uri): Thenable<KernelLike | undefined> };
}

/** A kernel status as the run tracker sees it (noise such as `starting` is dropped). */
export function observationOf(
  status: string,
  kernelName: string,
  language: string
): KernelObservation | undefined {
  if (status === 'restarting' || status === 'autorestarting') {
    return { state: 'restarting' };
  }
  if (status === 'dead') {
    return { state: 'dead' };
  }
  return status === 'idle' || status === 'busy'
    ? { state: 'started', kernelName, language }
    : undefined;
}

/**
 * Follows the notebook's kernel. A new kernel is a new object and the old
 * subscription hears nothing from it, so `refresh` (called when a run starts)
 * subscribes again to whatever kernel object is current.
 */
export function watchKernel(
  api: typeof vscode,
  uri: vscode.Uri,
  observe: (k: KernelObservation) => void,
  describe: () => { kernelName: string; language: string }
): { refresh(): void; dispose(): void } {
  let current: KernelLike | undefined;
  let subscription: vscode.Disposable | undefined;
  let disposed = false;
  const report = (status: string, language?: string) => {
    const d = describe();
    const o = observationOf(status, d.kernelName, language ?? d.language);
    if (o) {
      observe(o);
    }
  };
  const refresh = async () => {
    const ext = api.extensions.getExtension<JupyterApi>(JUPYTER_EXTENSION);
    const jupyter = ext && (ext.isActive ? ext.exports : await ext.activate());
    const kernel = await jupyter?.kernels.getKernel(uri);
    if (disposed || !kernel || kernel === current) {
      return;
    }
    subscription?.dispose();
    current = kernel;
    subscription = kernel.onDidChangeStatus(s => report(s, kernel.language));
    report(kernel.status, kernel.language);
  };
  return {
    refresh: () => void refresh().catch(() => undefined),
    dispose: () => {
      disposed = true;
      subscription?.dispose();
    }
  };
}
