import { CellIdentity } from '../../recording';
import { SeqIds } from './generate';

describe('CellIdentity (S5-U)', () => {
  test('stable ids pass through and nothing is translated', () => {
    const id = new CellIdentity(true, new SeqIds());
    expect(id.blaymId('abc')).toBe('abc');
    id.setAlias('abc', 'other');
    expect(id.blaymId('abc')).toBe('abc');
    expect(id.aliases()).toEqual({});
  });

  test('unstable ids use the table; only differing entries are aliases', () => {
    const id = new CellIdentity(false, new SeqIds());
    id.setAlias('m1', 'blaym-a');
    id.setAlias('m2', 'm2');
    expect(id.blaymId('m1')).toBe('blaym-a');
    expect(id.blaymId('m2')).toBe('m2');
    expect(id.blaymId('new')).toBe('new');
    expect(id.aliases()).toEqual({ m1: 'blaym-a' });
    expect(id.modelIdOf('blaym-a')).toBe('m1');
  });

  test('a new cell whose id equals a recorded Blaym id gets a fresh id (never two cells to one)', () => {
    const id = new CellIdentity(false, new SeqIds('fresh'));
    id.setAlias('m1', 'taken');
    expect(id.blaymId('taken')).toBe('fresh-1');
    expect(id.blaymId('taken')).toBe('fresh-1');
    expect(id.aliases()).toEqual({ m1: 'taken', taken: 'fresh-1' });
  });
});
