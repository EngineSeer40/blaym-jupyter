import { validateEnvelope } from 'blaym-jupyter-types';
import type { BlaymEventEnvelope } from 'blaym-jupyter-types';
import valid from '../../fixtures/envelope/valid-envelope.json';
import invalid from '../../fixtures/envelope/invalid-envelopes.json';

describe('shared envelope fixtures', () => {
  it('accepts the valid envelope', () => {
    const result = validateEnvelope(valid);
    expect(result.ok).toBe(true);
    if (result.ok) {
      const envelope: BlaymEventEnvelope = result.value;
      expect(envelope.type).toBe('s0_sample');
      expect(envelope.causal?.langgraph?.threadId).toBeDefined();
    }
  });

  it('has at least 6 invalid cases with unique names', () => {
    const names = invalid.map(c => c.name);
    expect(names.length).toBeGreaterThanOrEqual(6);
    expect(names.every(n => typeof n === 'string' && n.length > 0)).toBe(true);
    expect(new Set(names).size).toBe(names.length);
  });

  it.each(invalid.map(c => [c.name, c.envelope]))(
    'rejects invalid envelope: %s',
    (_name, envelope) => {
      expect(validateEnvelope(envelope).ok).toBe(false);
    }
  );
});
