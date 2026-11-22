/**
 * Test intensity for the generated navigation sequences, chosen with the
 * NAV_PROFILE environment variable: `default` (300 runs per property) or
 * `deep` (1,000 runs per property).
 */

declare const process: { env: { [key: string]: string | undefined } };

export const PROFILE: 'default' | 'deep' =
  process.env.NAV_PROFILE === 'deep' ? 'deep' : 'default';

export const NUM_RUNS = PROFILE === 'deep' ? 1000 : 300;

/** Parameters for fc.assert / fc.check. Failures print the seed and the shrunk case. */
export function fcParams(): { numRuns: number; verbose: 0 } {
  return { numRuns: NUM_RUNS, verbose: 0 };
}
