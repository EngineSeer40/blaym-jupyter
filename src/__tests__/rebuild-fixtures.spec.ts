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
  it('every event, outline and recorded result validates', () => {
    expect(files.length).toBeGreaterThanOrEqual(16);
    const check = (name: string, value: unknown, file: string) => {
      const r = validateContract(name, value);
      expect({ file, name, errors: r.ok ? [] : r.errors }).toEqual({
        file,
        name,
        errors: []
      });
    };
    for (const file of files) {
      const data = JSON.parse(readFileSync(join(DIR, file), 'utf8'));
      expect(data.events.length).toBeGreaterThan(0);
      expect(data.expectations.length).toBeGreaterThan(0);
      data.events.forEach((e: unknown) => check('BlaymEvent', e, file));
      check('HistoryOutline', data.outline, file);
      data.expectations.forEach((ex: { result: unknown }) =>
        check('RebuildResult', ex.result, file)
      );
    }
  });
});
