import type { CellType } from 'blaym-notebook-types';

export interface AlignCell {
  id: string;
  type: CellType;
  source: string;
}

export interface TipCell {
  cellId: string;
  type: CellType;
  code: string;
}

export interface Alignment {
  /** editor id to Blaym id, for every matched cell (a matched cell may have equal ids). */
  matches: Record<string, string>;
  /** Editor ids of cells with no recorded counterpart (new). */
  newModelIds: string[];
  /** Blaym ids of recorded cells with no counterpart on the page (deleted). */
  deletedBlaymIds: string[];
}

/**
 * Page-open alignment for a notebook with unstable ids: first the cells
 * with the same type and exact source, in order (a longest common subsequence),
 * then, between those anchors, the remaining cells of the same type by
 * position. A cell is never matched twice.
 */
export function alignCells(model: AlignCell[], tip: TipCell[]): Alignment {
  const n = model.length;
  const m = tip.length;
  const same = (i: number, j: number): boolean =>
    model[i].type === tip[j].type && model[i].source === tip[j].code;

  // Longest common subsequence on (type, source).
  const width = m + 1;
  const table = new Int32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i * width + j] = same(i, j)
        ? table[(i + 1) * width + j + 1] + 1
        : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  const pairs: Array<[number, number]> = [];
  for (let i = 0, j = 0; i < n && j < m;) {
    if (
      same(i, j) &&
      table[i * width + j] === table[(i + 1) * width + j + 1] + 1
    ) {
      pairs.push([i, j]);
      i++;
      j++;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      i++;
    } else {
      j++;
    }
  }

  const matchModel = new Array<number>(n).fill(-1);
  const matchTip = new Array<number>(m).fill(-1);
  pairs.forEach(([i, j]) => {
    matchModel[i] = j;
    matchTip[j] = i;
  });

  // Between anchors: the k-th unmatched cell of each side, when the types agree.
  const anchors: Array<[number, number]> = [[-1, -1], ...pairs, [n, m]];
  for (let a = 0; a + 1 < anchors.length; a++) {
    const [i0, j0] = anchors[a];
    const [i1, j1] = anchors[a + 1];
    const count = Math.min(i1 - i0 - 1, j1 - j0 - 1);
    for (let k = 1; k <= count; k++) {
      const i = i0 + k;
      const j = j0 + k;
      if (model[i].type === tip[j].type) {
        matchModel[i] = j;
        matchTip[j] = i;
      }
    }
  }

  const result: Alignment = {
    matches: {},
    newModelIds: [],
    deletedBlaymIds: []
  };
  model.forEach((c, i) => {
    if (matchModel[i] >= 0) {
      result.matches[c.id] = tip[matchModel[i]].cellId;
    } else {
      result.newModelIds.push(c.id);
    }
  });
  tip.forEach((c, j) => {
    if (matchTip[j] < 0) {
      result.deletedBlaymIds.push(c.cellId);
    }
  });
  return result;
}
