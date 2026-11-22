import { validateContract } from 'blaym-jupyter-types';

// Node's own modules, declared here because the project does not depend on @types/node.
declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');

const DIR: string = join(__dirname, '..', '..', 'fixtures', 'rebuild');
const files: string[] = readdirSync(DIR)
  .filter((f: string) => f.endsWith('.json'))
  .sort();

describe('shared rebuild fixtures', () => {
  it('finds the fixtures', () => {
    expect(files.length).toBeGreaterThanOrEqual(16);
  });

  it.each(files.map(f => [f]))(
    '%s: every event and recorded result validates',
    (file: string) => {
      const data = JSON.parse(readFileSync(join(DIR, file), 'utf8'));
      expect(data.events.length).toBeGreaterThan(0);
      expect(data.expectations.length).toBeGreaterThan(0);
      data.events.forEach((event: unknown, i: number) => {
        const r = validateContract('BlaymEvent', event);
        expect({
          file,
          event: i,
          ok: r.ok,
          errors: r.ok ? [] : r.errors
        }).toEqual({
          file,
          event: i,
          ok: true,
          errors: []
        });
      });
      data.expectations.forEach((ex: { result: unknown }, i: number) => {
        const r = validateContract('RebuildResult', ex.result);
        expect({
          file,
          result: i,
          ok: r.ok,
          errors: r.ok ? [] : r.errors
        }).toEqual({
          file,
          result: i,
          ok: true,
          errors: []
        });
      });
    }
  );
});
