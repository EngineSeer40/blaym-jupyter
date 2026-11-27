declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');

const ROOT: string = join(__dirname, '..', '..', '..');
const DIR: string = join(ROOT, 'src', 'recording');
const files: string[] = readdirSync(DIR)
  .filter((f: string) => f.endsWith('.ts'))
  .sort();

/** src/recording/ may import only blaym-notebook-types and itself. */
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
    spec => spec !== 'blaym-notebook-types' && !/^\.\/[a-z]+$/.test(spec)
  );
}

describe('src/recording boundary', () => {
  test('the real files import only blaym-notebook-types and each other', () => {
    expect(files.length).toBeGreaterThan(5);
    for (const f of files) {
      expect({
        f,
        bad: violations(readFileSync(join(DIR, f), 'utf8'))
      }).toEqual({ f, bad: [] });
    }
  });

  test('src/navigation and src/recording import no @jupyterlab and no adapters, and src/viewer imports no host or framework', () => {
    const nav = readdirSync(join(ROOT, 'src', 'navigation')).filter(
      (f: string) => f.endsWith('.ts')
    );
    expect(nav.length).toBeGreaterThan(0);
    const bad: string[] = [];
    for (const [dir, list] of [
      ['recording', files],
      ['navigation', nav]
    ] as [string, string[]][]) {
      for (const f of list) {
        for (const spec of importsOf(
          readFileSync(join(ROOT, 'src', dir, f), 'utf8')
        )) {
          if (spec.startsWith('@jupyterlab') || spec.includes('adapters')) {
            bad.push(`${dir}/${f}: ${spec}`);
          }
        }
      }
    }
    const view = readdirSync(join(ROOT, 'src', 'viewer')).filter((f: string) =>
      f.endsWith('.ts')
    );
    expect(view.length).toBeGreaterThan(0);
    for (const f of view) {
      for (const spec of importsOf(
        readFileSync(join(ROOT, 'src', 'viewer', f), 'utf8')
      )) {
        if (
          /^(@jupyterlab|@lumino|react)(\/|$)/.test(spec) ||
          spec === 'vscode'
        ) {
          bad.push(`viewer/${f}: ${spec}`);
        }
      }
    }
    expect(bad).toEqual([]);
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
