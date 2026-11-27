/**
 * How a cell is named on screen: its place in the notebook and its first line
 * ("cell 6 · # prompt: compare residuals…"), never a long id. Without a place
 * only the first line is shown; without either, just "cell".
 */
const MAX_LINE = 36;

export function firstLine(code: string | undefined): string {
  const line = (code ?? '').split('\n').find(l => l.trim() !== '');
  const text = line?.trim() ?? '';
  return text.length > MAX_LINE ? `${text.slice(0, MAX_LINE)}…` : text;
}

/** `index` counts from 0; the name counts from 1. */
export function cellName(
  code: string | undefined,
  index: number | null | undefined
): string {
  const line = firstLine(code);
  const place = index == null ? '' : `cell ${index + 1}`;
  return place && line ? `${place} · ${line}` : place || line || 'cell';
}

/** Looks a cell's name up: `code` and `index` of the cell in the shown state, if it is there. */
export type CellNamer = (cellId: string, entryIndex?: number | null) => string;
