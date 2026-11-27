import { matchStructure } from '../notebook';

const rec = (handle: number, index: number, text: string, id?: string) => ({
  handle,
  index,
  text,
  nbformatId: id
});

describe('matchStructure', () => {
  it('tells a move and a type change from delete plus insert', () => {
    // the same handle removed and added in one event
    expect(
      matchStructure([rec(1, 0, 'a', 'x')], [rec(1, 2, 'a', 'x')])
    ).toEqual({
      moves: [1],
      typeChanges: []
    });
    // same index, text and nbformat id under a new handle
    expect(
      matchStructure([rec(1, 0, 'a', 'x')], [rec(2, 0, 'a', 'x')])
    ).toEqual({
      moves: [],
      typeChanges: [{ from: 1, to: 2 }]
    });
    // a different id or text: delete plus insert
    expect(
      matchStructure([rec(1, 0, 'a', 'x')], [rec(2, 0, 'a', 'y')])
    ).toEqual({
      moves: [],
      typeChanges: []
    });
    expect(
      matchStructure([rec(1, 0, 'a', 'x')], [rec(2, 0, 'b', 'x')])
    ).toEqual({
      moves: [],
      typeChanges: []
    });
  });
});
