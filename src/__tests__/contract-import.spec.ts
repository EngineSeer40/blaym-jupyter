import { validateContract, validateEnvelope } from 'blaym-jupyter-types';

describe('contracts come from blaym-jupyter-types', () => {
  it('exposes the validators', () => {
    expect(typeof validateEnvelope).toBe('function');
    expect(validateContract('Nope', {}).ok).toBe(false);
  });
});
