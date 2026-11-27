declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');

const ROOT: string = join(__dirname, '..', '..', '..');
const DIR: string = join(ROOT, 'src', 'recording');
const files: string[] = readdirSync(DIR)
  .filter((f: string) => f.endsWith('.ts'))
  .sort();

/** src/recording/ may import only blaym-jupyter-types and itself. */
export function importsOf(source: string): string[] {
  const found: string[] = [];
  const patterns = [
    /\bfrom\s+['"]([^'"]+)['"]/g,
    /\bimport\s+['"]([^'"]+)['"]/g,
    /\brequire\(\s*['"]([^'"]+)['"]\s*\)/g,
    /\bimport\(\s*['"]([^'"]+)['"]\s*\)/g
  ];
  for (const re of patterns) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(source)) !== null) {
      found.push(m[1]);
    }
  }
  return found;
}

export function violations(source: string): string[] {
  return importsOf(source).filter(
    spec => spec !== 'blaym-jupyter-types' && !/^\.\/[a-z]+$/.test(spec)
  );
}

describe('src/recording boundary', () => {
  test('the real files import only blaym-jupyter-types and each other', () => {
    expect(files.length).toBeGreaterThan(5);
    for (const f of files) {
      expect({
        f,
        bad: violations(readFileSync(join(DIR, f), 'utf8'))
      }).toEqual({ f, bad: [] });
    }
  });

  test('a planted @jupyterlab import is caught', () => {
    expect(violations(`import { x } from '@jupyterlab/application';`)).toEqual([
      '@jupyterlab/application'
    ]);
    expect(violations(`import('@lumino/signaling')`)).toEqual([
      '@lumino/signaling'
    ]);
    expect(violations(`import { y } from '../navigation';`)).toEqual([
      '../navigation'
    ]);
  });
});
