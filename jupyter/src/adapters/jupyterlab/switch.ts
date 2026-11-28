import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';
import type {
  HistoryOutline,
  NotebookCell,
  OutputItem,
  RebuildResult,
  SetActiveBranchResponse
} from 'blaym-notebook-types';
import { ROUTE_BASE, ROUTES, routePath } from 'blaym-notebook-core';
import type { RouteName } from 'blaym-notebook-core';

/** Measured in Probe B: the page's late change signals have all arrived by then. */
export const QUIET_MS = 300;

export type SwitchStatus = 'ok' | 'cancelled' | 'failed';

export interface SwitchResult {
  status: SwitchStatus;
  reason?: string;
}

/** The target branch's latest state, ready to put into the notebook. */
export interface SwitchTarget {
  name: string;
  /** The recorded cells, for the recorder's new baseline. */
  cells: NotebookCell[];
  /** The same cells as nbformat cell JSON (outputs of the latest run, Blaym's ids). */
  nbCells: Record<string, unknown>[];
  /** How many outputs came back in a simplified form (the store kept only a preview). */
  simplified?: number;
}

/** The shared notebook model as far as the switch touches it. */
export interface SwitchShared {
  cells: ReadonlyArray<{ toJSON(): Record<string, unknown> }>;
  transact(f: () => void): void;
  deleteCellRange(from: number, to: number): void;
  insertCells(index: number, cells: Record<string, unknown>[]): unknown;
  clearUndoHistory(): void;
}

export interface SwitchDeps {
  /** Names the notebook: one switch at a time per key. */
  key: string;
  /** A cell is running now: the switch is refused before anything changes. */
  busy(): boolean;
  shared(): SwitchShared;
  /** Send typing still waiting in the recorder. */
  flushTyping(): void;
  /** The notebook has unsaved changes. */
  dirty(): boolean;
  /** "Save and switch" is true, "Cancel" false. */
  confirm(): Promise<boolean>;
  save(): Promise<void>;
  fetchTarget(branchId: string): Promise<SwitchTarget>;
  setActive(branchId: string): Promise<{ ok: boolean; reason?: string }>;
  /** The recorder's switching flag, read at event time. */
  setSwitching(on: boolean): void;
  wait(ms: number): Promise<void>;
  /** The recorder takes the target as its view of the notebook (no events). */
  adoptBaseline(cells: NotebookCell[]): void;
  /** Restart the kernel through the session context; false when there is none. */
  restartKernel(): Promise<boolean>;
  refocus(): void;
  notify(severity: 'info' | 'warning' | 'error', text: string): void;
}

function say(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * Moves the open notebook to another branch's latest state, in place.
 * The order is fixed (spec S8, section 0); every failure before `setActive`
 * leaves the notebook as it was, and a failure of `setActive` restores it.
 */
const running = new Set<string>();

export class BranchSwitch {
  constructor(private readonly d: SwitchDeps) {}

  async run(branchId: string): Promise<SwitchResult> {
    if (running.has(this.d.key)) {
      return { status: 'failed', reason: 'A switch is already running' };
    }
    if (this.d.busy()) {
      return {
        status: 'failed',
        reason:
          'A cell is still running. Switch after it finishes; nothing was changed.'
      };
    }
    running.add(this.d.key);
    try {
      return await this.steps(branchId);
    } finally {
      running.delete(this.d.key);
    }
  }

  private async steps(branchId: string): Promise<SwitchResult> {
    const d = this.d;
    // 1. Pending typing goes out first.
    d.flushTyping();
    // 2. Unsaved edits: ask.
    if (d.dirty()) {
      if (!(await d.confirm())) {
        return { status: 'cancelled' };
      }
      try {
        await d.save();
      } catch (e) {
        return this.fail(`Could not save the notebook (${say(e)})`);
      }
      d.flushTyping();
    }
    // 3. The target state.
    let target: SwitchTarget;
    try {
      target = await d.fetchTarget(branchId);
    } catch (e) {
      return this.fail(`Could not read the branch (${say(e)})`);
    }
    // 4. A copy for rollback, then the flag.
    const shared = d.shared();
    const copy = shared.cells.map(c => c.toJSON());
    d.setSwitching(true);
    try {
      // 5. Replace in one edit, clear undo, save.
      try {
        this.replace(shared, target.nbCells);
        await d.save();
      } catch (e) {
        await this.restore(shared, copy);
        return this.fail(`${say(e)}`);
      }
      // 6. Recording moves only now that the notebook holds the branch.
      let active: { ok: boolean; reason?: string };
      try {
        active = await d.setActive(branchId);
      } catch (e) {
        active = { ok: false, reason: say(e) };
      }
      if (!active.ok) {
        await this.restore(shared, copy);
        return this.fail(active.reason ?? 'The branch could not be activated');
      }
      // 7. Quiet period, then the recorder's new baseline; the flag closes below.
      await d.wait(QUIET_MS);
      d.adoptBaseline(target.cells);
    } finally {
      d.setSwitching(false);
    }
    d.refocus();
    // 8. A fresh kernel.
    let restarted = false;
    try {
      restarted = await d.restartKernel();
    } catch (e) {
      d.notify(
        'warning',
        `Switched to ${target.name}, but the kernel did not restart (${say(e)}). Variables in memory still belong to the previous branch.`
      );
      return { status: 'ok' };
    }
    // 9. Say what was not restored.
    const approx = target.simplified
      ? ' Some outputs are shown in a simplified form: Blaym kept only their text.'
      : '';
    d.notify(
      'info',
      (restarted
        ? `Switched to ${target.name}. Variables are not restored: the kernel was restarted.`
        : `Switched to ${target.name}. Variables are not restored: there was no running kernel.`) +
        approx
    );
    return { status: 'ok' };
  }

  private replace(
    shared: SwitchShared,
    cells: Record<string, unknown>[]
  ): void {
    shared.transact(() => {
      shared.deleteCellRange(0, shared.cells.length);
      shared.insertCells(0, cells);
    });
    shared.clearUndoHistory();
  }

  /** Put the copy back and save; the flag is still open, so none of it is recorded. */
  private async restore(
    shared: SwitchShared,
    copy: Record<string, unknown>[]
  ): Promise<void> {
    try {
      this.replace(shared, copy);
      await this.d.save();
      await this.d.wait(QUIET_MS);
    } catch {
      // the copy is in the page either way; the next save keeps it
    }
  }

  private fail(reason: string): SwitchResult {
    const text = `Switch failed: ${reason}. You are still on your previous branch.`;
    this.d.notify('error', text);
    return { status: 'failed', reason: text };
  }
}

/* ------------------------------------------------------------------ */
/* The service side: the target state and the active branch.           */
/* ------------------------------------------------------------------ */

type MakeRequest = typeof ServerConnection.makeRequest;

function placeholderOutput(
  item: OutputItem,
  runCount: number | undefined
): Record<string, unknown> {
  const text = item.preview;
  switch (item.outputType) {
    case 'stream':
      return { output_type: 'stream', name: 'stdout', text };
    case 'error':
      return { output_type: 'error', ename: '', evalue: '', traceback: [text] };
    case 'execute_result':
      return {
        output_type: 'execute_result',
        execution_count: runCount ?? null,
        data: { 'text/plain': text },
        metadata: {}
      };
    default:
      return {
        output_type: 'display_data',
        data: { 'text/plain': text },
        metadata: {}
      };
  }
}

/** Reads the target branch's tip from the service and builds the notebook cells from it. */
export class ServiceTarget {
  constructor(
    private readonly explorationId: string,
    private readonly settings: ServerConnection.ISettings,
    private readonly makeRequest: MakeRequest = ServerConnection.makeRequest
  ) {}

  private async call<T>(
    name: RouteName,
    params: Record<string, string>,
    body?: unknown
  ): Promise<T> {
    const post = ROUTES[name].method === 'POST';
    const path = routePath(name, params);
    const query = post ? '' : URLExt.objectToQueryString(params);
    const res = await this.makeRequest(
      URLExt.join(this.settings.baseUrl, ROUTE_BASE, path) + query,
      post
        ? {
            method: 'POST',
            body: JSON.stringify(body),
            headers: { 'Content-Type': 'application/json' }
          }
        : { method: ROUTES[name].method },
      this.settings
    );
    if (!res.ok) {
      throw new Error(`${name} answered ${res.status}`);
    }
    return (name === 'get_blob' ? res.arrayBuffer() : res.json()) as Promise<T>;
  }

  async fetchTarget(branchId: string): Promise<SwitchTarget> {
    const id = this.explorationId;
    const outline = await this.call<HistoryOutline>('outline', {
      explorationId: id
    });
    const branch = outline.branches.find(b => b.branchId === branchId);
    if (!branch) {
      throw new Error('the branch does not exist');
    }
    const tick =
      branch.ownTicks.length > 0
        ? branch.ownTicks[branch.ownTicks.length - 1]
        : branch.parentTick;
    if (tick === undefined) {
      throw new Error('the branch has no recorded state');
    }
    const result = await this.call<RebuildResult>('state', {
      explorationId: id,
      branchId,
      tick: String(tick)
    });
    if (result.status !== 'ok') {
      throw new Error('the branch state cannot be rebuilt');
    }
    const cells = result.state.notebook.cells;
    const nbCells: Record<string, unknown>[] = [];
    this.simplified = 0;
    for (const c of cells) {
      nbCells.push(await this.nbCell(c, result.state.runs));
    }
    return {
      name: branch.parentBranchId ? branch.name : 'main',
      cells,
      nbCells,
      simplified: this.simplified
    };
  }

  private simplified = 0;

  private async nbCell(
    c: NotebookCell,
    runs: { cellId?: string; startedTick: number; runCount?: number }[]
  ): Promise<Record<string, unknown>> {
    const base = {
      id: c.cellId,
      cell_type: c.cellType,
      source: c.code,
      metadata: c.metadata ?? {}
    };
    if (c.cellType !== 'code') {
      return base;
    }
    // the count the ghost book shows: the latest run's, else the cell's own
    let latest: { startedTick: number; runCount?: number } | undefined;
    for (const r of runs) {
      if (
        r.cellId === c.cellId &&
        (!latest || r.startedTick >= latest.startedTick)
      ) {
        latest = r;
      }
    }
    const count = latest?.runCount ?? c.runCount;
    const group = [...c.outputs].reverse().find(g => g.items.length > 0);
    const outputs: Record<string, unknown>[] = [];
    for (const item of group?.items ?? []) {
      outputs.push(await this.output(item, count));
    }
    return { ...base, outputs, execution_count: count ?? null };
  }

  /** A stored output: the whole one from its blob, else the preview as plain text. */
  private async output(
    item: OutputItem,
    runCount: number | undefined
  ): Promise<Record<string, unknown>> {
    const inline =
      item.sizeBytes === new TextEncoder().encode(item.preview).length;
    if (!inline || item.truncated) {
      try {
        const bytes = await this.call<ArrayBuffer>('get_blob', {
          explorationId: this.explorationId,
          contentHash: item.contentHash
        });
        const full = JSON.parse(new TextDecoder().decode(bytes)) as Record<
          string,
          unknown
        >;
        if (typeof full.output_type === 'string') {
          if (full.output_type === 'execute_result') {
            full.execution_count ??= runCount ?? null;
          }
          return full;
        }
      } catch {
        // fall through to the preview
      }
    }
    // an inline stream or result is its plain text; the rest lost structure
    if (
      !inline ||
      item.truncated ||
      item.outputType === 'error' ||
      item.outputType === 'display_data'
    ) {
      this.simplified += 1;
    }
    return placeholderOutput(item, runCount);
  }

  async setActive(branchId: string): Promise<{ ok: boolean; reason?: string }> {
    const r = await this.call<SetActiveBranchResponse>(
      'set_active_branch',
      {},
      { explorationId: this.explorationId, branchId }
    );
    return r.status === 'ok' ? { ok: true } : { ok: false, reason: r.detail };
  }
}
