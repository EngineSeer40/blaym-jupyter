/**
 * @jest-environment jsdom
 */
import { renderContext } from '../../viewer/context';
import type { ContextEntry } from '../../viewer/context';

const entry = (over: Partial<ContextEntry>): ContextEntry => ({
  tick: 41,
  timeMs: 1763002106727,
  kind: 'edit',
  author: 'person',
  confidence: 'known',
  cellId: 'c4',
  runId: null,
  ...over
});

describe('viewer context panel', () => {
  test('an unknown kind shows as a generic step with its kind name and fields; a known kind shows its label and author chip', () => {
    const panel = document.createElement('div');

    renderContext(panel, entry({ kind: 'made_up_kind', author: 'unknown' }));
    expect(panel.textContent).toContain('made_up_kind');
    expect(panel.textContent).toContain('generic step');
    expect(panel.textContent).toContain('cellId');
    expect(panel.textContent).toContain('c4');
    expect(panel.querySelector('svg.bv-icon')).not.toBeNull();

    renderContext(
      panel,
      entry({ kind: 'edit', author: 'ai', confidence: 'guess' })
    );
    expect(panel.textContent).toContain('Code edit');
    expect(panel.querySelector('.bv-chip-ai')?.textContent).toBe('AI');
    expect(panel.textContent).toContain('guess');
  });
});
