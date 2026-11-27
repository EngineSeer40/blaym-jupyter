import { authors } from '../../viewer/authors';
import type { ContextEntry } from '../../viewer/context';

let tick = 0;
const e = (kind: string, over: Partial<ContextEntry> = {}): ContextEntry => ({
  tick: ++tick,
  timeMs: tick * 1000,
  kind,
  author: 'person',
  confidence: 'known',
  cellId: null,
  runId: null,
  ...over
});

describe('viewer cell authors', () => {
  test('the latest edit-like entry wins, runs do not count, a guess stays a guess, no entry is unknown', () => {
    const index = [
      e('edit', { cellId: 'a' }),
      e('edit', { cellId: 'a', author: 'ai', confidence: 'known' }),
      e('run_ok', { cellId: 'a', author: 'unknown', confidence: null }),
      e('edit', { cellId: 'b', author: 'ai', confidence: 'guess' }),
      e('edit', { cellId: 'a' })
    ];
    const at4 = authors(index, 4);
    expect(at4.get('a')).toEqual({ author: 'ai', confidence: 'known' });
    expect(at4.get('b')).toEqual({ author: 'ai', confidence: 'guess' });
    expect(at4.get('c')).toBeUndefined();
    expect(authors(index, 5).get('a')?.author).toBe('person');
  });
});
