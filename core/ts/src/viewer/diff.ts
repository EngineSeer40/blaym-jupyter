/**
 * The diff between two rebuilt states, by cell id. Pure: `diff(base, shown)`
 * says what the shown notebook has that the base has not (added), what the
 * base has that the shown one lacks (deleted, with the index it had in the
 * base), which shared cells changed place, which cell code changed (with the
 * shown lines that differ) and which outputs changed. No library: a plain
 * longest-common-subsequence, with a cap so a huge cell cannot stall the page.
 */
import type {
  NotebookCell,
  RebuiltState,
  RunOutputs
} from 'blaym-notebook-types';

/** Above this many lines (either side), a changed cell is marked changed as a whole. */
export const MAX_DIFF_LINES = 2000;
const MAX_CELLS = 2000;

export interface CellMark {
  /** In the shown notebook, not in the base. */
  added: boolean;
  /** Its place among the cells both states share differs. */
  moved: boolean;
  /** The code differs. */
  codeChanged: boolean;
  /** The shown lines (0-based) that are not in the base; empty when `wholeCell`. */
  changedLines: number[];
  /** Too large to compare line by line: the whole cell counts as changed. */
  wholeCell: boolean;
  /** The items of the shown outputs differ from the base's. */
  outputsChanged: boolean;
}

export interface DeletedCell {
  cell: NotebookCell;
  /** The index the cell had in the base. */
  index: number;
}

export interface DiffResult {
  /** A mark for every shown cell that has any; unmarked cells are absent. */
  cells: Map<string, CellMark>;
  /** In the base, not in the shown notebook, in base order. */
  deleted: DeletedCell[];
}

/** The outputs Jupyter shows: those of the cell's latest run that has any. */
export function latestOutputs(cell: NotebookCell): RunOutputs | undefined {
  for (let i = cell.outputs.length - 1; i >= 0; i--) {
    if (cell.outputs[i].items.length > 0) {
      return cell.outputs[i];
    }
  }
  return undefined;
}

function outputKey(cell: NotebookCell): string {
  return (
    latestOutputs(cell)
      ?.items.map(i => i.contentHash)
      .join(',') ?? ''
  );
}

/**
 * Which elements of `b` are in a longest common subsequence with `a`
 * (true = kept). Undefined when the work would pass `limit` steps.
 */
export function keptInB(
  a: readonly string[],
  b: readonly string[],
  limit = 4_000_000
): boolean[] | undefined {
  const n = a.length;
  const m = b.length;
  if ((n + 1) * (m + 1) > limit) {
    return undefined;
  }
  const w = m + 1;
  const t = new Uint32Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      t[i * w + j] =
        a[i] === b[j]
          ? t[(i + 1) * w + j + 1] + 1
          : Math.max(t[(i + 1) * w + j], t[i * w + j + 1]);
    }
  }
  const kept = new Array<boolean>(m).fill(false);
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      kept[j] = true;
      i++;
      j++;
    } else if (t[(i + 1) * w + j] >= t[i * w + j + 1]) {
      i++;
    } else {
      j++;
    }
  }
  return kept;
}

function blank(): CellMark {
  return {
    added: false,
    moved: false,
    codeChanged: false,
    changedLines: [],
    wholeCell: false,
    outputsChanged: false
  };
}

export function diff(base: RebuiltState, shown: RebuiltState): DiffResult {
  const baseCells = base.notebook.cells;
  const shownCells = shown.notebook.cells;
  const inBase = new Map(baseCells.map((c, i) => [c.cellId, { c, i }]));
  const inShown = new Set(shownCells.map(c => c.cellId));
  const cells = new Map<string, CellMark>();
  const mark = (id: string): CellMark => {
    let m = cells.get(id);
    if (!m) {
      m = blank();
      cells.set(id, m);
    }
    return m;
  };

  // Moved: shared cells outside the longest common run of the two orders.
  const sharedBase = baseCells.map(c => c.cellId).filter(id => inShown.has(id));
  const sharedShown = shownCells
    .map(c => c.cellId)
    .filter(id => inBase.has(id));
  const kept =
    sharedShown.length <= MAX_CELLS
      ? keptInB(sharedBase, sharedShown)
      : undefined;
  kept?.forEach((k, i) => {
    if (!k) {
      mark(sharedShown[i]).moved = true;
    }
  });

  for (const cell of shownCells) {
    const was = inBase.get(cell.cellId)?.c;
    if (!was) {
      mark(cell.cellId).added = true;
      continue;
    }
    if (was.code !== cell.code) {
      const m = mark(cell.cellId);
      m.codeChanged = true;
      const a = was.code.split('\n');
      const b = cell.code.split('\n');
      const keep =
        a.length > MAX_DIFF_LINES || b.length > MAX_DIFF_LINES
          ? undefined
          : keptInB(a, b);
      if (keep) {
        keep.forEach((k, i) => {
          if (!k) {
            m.changedLines.push(i);
          }
        });
      } else {
        m.wholeCell = true;
      }
    }
    if (outputKey(was) !== outputKey(cell)) {
      mark(cell.cellId).outputsChanged = true;
    }
  }

  const deleted = baseCells
    .map((cell, index) => ({ cell, index }))
    .filter(d => !inShown.has(d.cell.cellId));
  return { cells, deleted };
}
