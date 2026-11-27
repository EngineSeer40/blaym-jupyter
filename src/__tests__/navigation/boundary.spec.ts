// Node's own modules, declared here because the project does not depend on @types/node.
declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');

const DIR: string = join(__dirname, '..', '..', 'navigation');
const files: string[] = readdirSync(DIR)
  .filter((f: string) => f.endsWith('.ts'))
  .sort();

const FORBIDDEN = [/^@jupyterlab\//, /^@lumino\//, /^react($|\/)/];

function importsOf(source: string): string[] {
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

function allowed(spec: string): boolean {
  if (FORBIDDEN.some(re => re.test(spec))) {
    return false;
  }
  if (spec === 'blaym-jupyter-types') {
    return true;
  }
  // Only siblings inside src/navigation/.
  return /^\.\/[A-Za-z0-9_-]+$/.test(spec);
}

describe('src/navigation stays free of UI and outside code', () => {
  it('every navigation file imports only what it may', () => {
    expect(files).toContain('line.ts');
    for (const file of files) {
      const source = readFileSync(join(DIR, file), 'utf8');
      const bad = importsOf(source).filter(s => !allowed(s));
      expect({ file, bad }).toEqual({ file, bad: [] });
    }
  });
});
