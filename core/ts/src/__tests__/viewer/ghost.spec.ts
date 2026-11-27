/**
 * @jest-environment jsdom
 */
import type { RebuiltState } from 'blaym-notebook-types';
import { renderGhostBook } from '../../viewer/ghost';

const state = (): RebuiltState =>
  ({
    schemaVersion: 2,
    position: { branchId: 'b', tick: 9, lastEventTick: 9, branchCount: 9 },
    notebook: {
      path: 'n.ipynb',
      metadata: {},
      cells: [
        {
          cellId: 'c1',
          cellType: 'code',
          code: '<img src=x onerror=alert(1)>\nx = 1',
          revision: 1,
          metadata: {},
          runCount: 3,
          outputs: [
            {
              runId: 'r1',
              items: [
                {
                  outputType: 'stream',
                  mimeTypes: ['text/plain'],
                  contentHash: 'h1',
                  preview: 'hello',
                  truncated: true,
                  sizeBytes: 4096
                },
                {
                  outputType: 'made_up',
                  mimeTypes: [],
                  contentHash: 'h2',
                  preview: '',
                  truncated: false,
                  sizeBytes: 1
                }
              ]
            }
          ]
        }
      ]
    },
    runs: [
      {
        runId: 'r1',
        cellId: 'c1',
        cellRevision: 1,
        runCount: 3,
        codeLink: 'by_time',
        status: 'ok',
        startedTick: 5
      }
    ]
  }) as unknown as RebuiltState;

describe('viewer ghost book', () => {
  test('shows In/Out numbers, code as text, run status and a placeholder for an unknown output', () => {
    const target = document.createElement('div');
    const focused: string[] = [];
    renderGhostBook(target, {
      state: state(),
      header: 'notebook at x · tick 9',
      loading: false,
      focusedCell: null,
      onFocus: id => focused.push(id)
    });
    const text = target.textContent ?? '';
    expect(text).toContain('In [3]:');
    expect(text).toContain('Out [3]:');
    expect(text).toContain('ok · matched by time');
    expect(text).toContain('truncated');
    expect(text).toContain('unknown output (made_up)');
    expect(target.querySelector('.bv-gcell-code')?.textContent).toBe(
      '<img src=x onerror=alert(1)>\nx = 1'
    );
    expect(target.querySelector('img')).toBeNull();
    target.querySelector<HTMLElement>('.bv-gcell')?.click();
    expect(focused).toEqual(['c1']);
  });
});
