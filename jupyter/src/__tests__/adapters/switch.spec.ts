import type { NotebookCell } from 'blaym-notebook-types';
import { BranchSwitch } from '../../adapters/jupyterlab/switch';
import type {
  SwitchDeps,
  SwitchShared,
  SwitchTarget
} from '../../adapters/jupyterlab/switch';

type Cell = Record<string, unknown>;

/** A notebook model and a recorder that only hears changes while the flag is down. */
let n = 0;
function setup(opts: {
  dirty?: boolean;
  confirm?: boolean;
  refuse?: boolean;
  busy?: boolean;
  simplified?: number;
}) {
  const log: string[] = [];
  const recorded: string[] = [];
  let flag = false;
  let cells: Cell[] = [{ id: 'old', cell_type: 'code', source: 'a = 1' }];
  const change = (what: string): void => {
    log.push(what);
    if (!flag) {
      recorded.push(what);
    }
  };
  const shared: SwitchShared = {
    get cells() {
      return cells.map(c => ({ toJSON: () => c }));
    },
    transact: f => f(),
    deleteCellRange: () => {
      cells = [];
      change('delete');
    },
    insertCells: (_i, added) => {
      cells = [...cells, ...added];
      change('insert');
    },
    clearUndoHistory: () => log.push('clear undo')
  };
  const target: SwitchTarget = {
    name: 'branch from 10:00',
    cells: [] as NotebookCell[],
    nbCells: [{ id: 'new', cell_type: 'code', source: 'b = 2' }],
    simplified: opts.simplified
  };
  const deps: SwitchDeps = {
    key: `nb${n++}`,
    busy: () => !!opts.busy,
    shared: () => shared,
    flushTyping: () => log.push('flush'),
    dirty: () => !!opts.dirty,
    confirm: async () => (log.push('dialog'), opts.confirm ?? true),
    save: async () => void log.push('save'),
    fetchTarget: async () => (log.push('fetch'), target),
    setActive: async () => (
      log.push('set active'),
      opts.refuse ? { ok: false, reason: 'no such branch' } : { ok: true }
    ),
    setSwitching: on => {
      flag = on;
      log.push(on ? 'flag open' : 'flag closed');
    },
    wait: async ms => void log.push(`wait ${ms}`),
    adoptBaseline: () => log.push('adopt'),
    restartKernel: async () => (log.push('restart'), true),
    refocus: () => log.push('focus'),
    notify: (sev, text) => log.push(`${sev}: ${text}`)
  };
  return {
    run: () => new BranchSwitch(deps).run('b2'),
    log,
    recorded,
    cells: () => cells
  };
}

describe('the branch switch', () => {
  it('runs the steps in order and records nothing while the flag is open', async () => {
    const t = setup({ dirty: true, simplified: 2 });
    const first = t.run();
    const second = await t.run();
    expect(second.status).toBe('failed');
    expect(second.reason).toContain('already running');
    expect(await first).toEqual({ status: 'ok' });
    expect(t.log).toEqual([
      'flush',
      'dialog',
      'save',
      'flush',
      'fetch',
      'flag open',
      'delete',
      'insert',
      'clear undo',
      'save',
      'set active',
      'wait 300',
      'adopt',
      'flag closed',
      'focus',
      'restart',
      'info: Switched to branch from 10:00. Variables are not restored: the kernel was restarted. Some outputs are shown in a simplified form: Blaym kept only their text.'
    ]);
    expect(t.recorded).toEqual([]);
    expect(t.cells().map(c => c.id)).toEqual(['new']);
  });

  it('puts the old cells back and stays on the branch when set-active fails', async () => {
    const t = setup({ refuse: true });
    const r = await t.run();
    expect(r.status).toBe('failed');
    expect(r.reason).toContain('no such branch');
    expect(r.reason).toContain('still on your previous branch');
    expect(t.cells().map(c => c.id)).toEqual(['old']);
    expect(t.recorded).toEqual([]);
    expect(t.log).not.toContain('adopt');
    expect(t.log).not.toContain('restart');
    expect(t.log.indexOf('flag closed')).toBeGreaterThan(
      t.log.lastIndexOf('insert')
    );
  });

  it('changes nothing when the user cancels the dialog', async () => {
    const t = setup({ dirty: true, confirm: false });
    expect(await t.run()).toEqual({ status: 'cancelled' });
    expect(t.log).toEqual(['flush', 'dialog']);
    expect(t.cells().map(c => c.id)).toEqual(['old']);
    const busy = setup({ dirty: true, busy: true });
    const r = await busy.run();
    expect(r.reason).toContain('still running');
    expect(busy.log).toEqual([]);
  });
});
