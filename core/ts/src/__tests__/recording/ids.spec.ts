import { CellIdentity } from '../../recording';
import { SeqIds } from './generate';

describe('CellIdentity', () => {
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
});
