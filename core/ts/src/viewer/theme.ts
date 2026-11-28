/**
 * The viewer's colours and fonts. Every `--bv-*` token has a source in each
 * host (JupyterLab `--jp-*`, VS Code `--vscode-*`) and a light and a dark
 * fallback; the dark fallback follows the landing page tokens
 * (`docs/design/2026-11-27-landing-tokens.css`). This file is the only place
 * with literal colours.
 *
 * The `--jp-*` names the JupyterLab host must send in a `blaym:theme` message
 * are `JP_VARIABLES` below.
 */
import type { HostPort, ThemeMessage } from './host';
import { DIFF_TOKENS, KIND_TOKENS, THEME_TOKENS, TREE_TOKENS } from './tokens';
export {
  DIFF_TOKENS,
  JP_VARIABLES,
  KIND_TOKENS,
  THEME_TOKENS,
  TREE_TOKENS
} from './tokens';
export type { ThemeToken } from './tokens';

export type ThemeKind = 'light' | 'dark';

export interface ThemeSource {
  kind: ThemeKind;
  /** Which host names `values` is keyed by; `fallback` uses none. */
  from: 'jp' | 'vscode' | 'fallback';
  values: Record<string, string>;
}

/** Sets every `--bv-*` token and `data-bv-theme` on `root`. A missing value uses the kind's fallback. */
export function applyTheme(root: HTMLElement, source: ThemeSource): void {
  for (const t of THEME_TOKENS) {
    const name =
      source.from === 'jp' ? t.jp : source.from === 'vscode' ? t.vscode : null;
    const given = name ? source.values[name]?.trim() : undefined;
    root.style.setProperty(t.token, given ? given : t[source.kind]);
  }
  for (const k of KIND_TOKENS) {
    root.style.setProperty(k.token, k[source.kind]);
  }
  for (const k of DIFF_TOKENS) {
    root.style.setProperty(k.token, k[source.kind]);
  }
  for (const k of TREE_TOKENS) {
    root.style.setProperty(k.token, k[source.kind]);
  }
  root.setAttribute('data-bv-theme', source.kind);
}

const VSCODE_CLASSES: Record<string, ThemeKind> = {
  'vscode-light': 'light',
  'vscode-high-contrast-light': 'light',
  'vscode-dark': 'dark',
  'vscode-high-contrast': 'dark'
};

function vscodeKind(body: HTMLElement): ThemeKind | undefined {
  // The light high-contrast class carries `vscode-high-contrast` too, so test it first.
  for (const cls of [
    'vscode-high-contrast-light',
    'vscode-light',
    'vscode-dark',
    'vscode-high-contrast'
  ]) {
    if (body.classList.contains(cls)) {
      return VSCODE_CLASSES[cls];
    }
  }
  return undefined;
}

/**
 * Applies the theme now and again on every change: the latest `blaym:theme`
 * message wins, then a VS Code body class, then `prefers-color-scheme`.
 * Returns a function that stops watching.
 */
export function startTheme(win: Window, port: HostPort): () => void {
  const doc = win.document;
  const root = doc.documentElement;
  let fromHost: ThemeMessage | undefined;

  const apply = () => {
    if (fromHost) {
      applyTheme(root, {
        kind: fromHost.kind,
        from: 'jp',
        values: fromHost.values ?? {}
      });
      return;
    }
    const kind = doc.body ? vscodeKind(doc.body) : undefined;
    if (kind && doc.body) {
      const style = win.getComputedStyle(doc.body);
      const values: Record<string, string> = {};
      for (const t of THEME_TOKENS) {
        if (t.vscode) {
          values[t.vscode] = style.getPropertyValue(t.vscode);
        }
      }
      applyTheme(root, { kind, from: 'vscode', values });
      return;
    }
    const dark = win.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(root, {
      kind: dark ? 'dark' : 'light',
      from: 'fallback',
      values: {}
    });
  };

  const stopListening = port.listen(ev => {
    const m = ev.data as Partial<ThemeMessage> | null;
    if (
      m &&
      typeof m === 'object' &&
      m.type === 'blaym:theme' &&
      (m.kind === 'light' || m.kind === 'dark')
    ) {
      fromHost = m as ThemeMessage;
      apply();
    }
  });
  const observer = new MutationObserver(apply);
  if (doc.body) {
    observer.observe(doc.body, {
      attributes: true,
      attributeFilter: ['class']
    });
  }
  const media = win.matchMedia('(prefers-color-scheme: dark)');
  media.addEventListener('change', apply);
  apply();

  return () => {
    stopListening();
    observer.disconnect();
    media.removeEventListener('change', apply);
  };
}
