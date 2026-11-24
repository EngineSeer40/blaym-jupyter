declare const require: (name: string) => any;
declare const __dirname: string;
const { readdirSync, readFileSync } = require('fs');
const { join } = require('path');
const ts = require('typescript');

jest.setTimeout(120000);

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

function compilerOptions() {
  const cfgPath = join(ROOT, 'tsconfig.recording.json');
  const cfg = ts.readConfigFile(cfgPath, ts.sys.readFile);
  return ts.parseJsonConfigFileContent(cfg.config, ts.sys, ROOT);
}

/** Compile the given files plus an optional in-memory file under tsconfig.recording.json. */
function diagnostics(extra?: { name: string; text: string }): string[] {
  const parsed = compilerOptions();
  const host = ts.createCompilerHost(parsed.options);
  const realGet = host.getSourceFile.bind(host);
  const realExists = host.fileExists.bind(host);
  const realRead = host.readFile.bind(host);
  if (extra) {
    host.getSourceFile = (name: string, lang: any, ...rest: any[]) =>
      name === extra.name
        ? ts.createSourceFile(name, extra.text, lang)
        : realGet(name, lang, ...rest);
    host.fileExists = (n: string) => n === extra.name || realExists(n);
    host.readFile = (n: string) => (n === extra.name ? extra.text : realRead(n));
  }
  const roots = files.map((f: string) => join(DIR, f));
  if (extra) {
    roots.push(extra.name);
  }
  const program = ts.createProgram(roots, parsed.options, host);
  return ts
    .getPreEmitDiagnostics(program)
    .map((d: any) => `${d.file ? d.file.fileName : ''}: ${ts.flattenDiagnosticMessageText(d.messageText, '\n')}`);
}

describe('src/recording boundary', () => {
  test('the real files import only blaym-jupyter-types and each other', () => {
    expect(files.length).toBeGreaterThan(5);
    for (const f of files) {
      expect({ f, bad: violations(readFileSync(join(DIR, f), 'utf8')) }).toEqual({ f, bad: [] });
    }
  });

  test('a planted @jupyterlab import is caught', () => {
    expect(violations(`import { x } from '@jupyterlab/application';`)).toEqual(['@jupyterlab/application']);
    expect(violations(`import('@lumino/signaling')`)).toEqual(['@lumino/signaling']);
    expect(violations(`import { y } from '../navigation';`)).toEqual(['../navigation']);
  });

  test('the real files compile with no dom lib (tsconfig.recording.json)', () => {
    expect(compilerOptions().options.lib.join(' ')).not.toMatch(/dom/i);
    expect(diagnostics()).toEqual([]);
  });

  test('a planted window, document, setTimeout and fetch use fails to compile', () => {
    const name = join(DIR, '__planted.ts');
    for (const use of ['window.location', 'document.title', 'setTimeout(() => 1, 1)', 'fetch("/x")', 'navigator.userAgent']) {
      const d = diagnostics({ name, text: `export const x = ${use};` });
      expect({ use, failed: d.length > 0 }).toEqual({ use, failed: true });
    }
  });
});
