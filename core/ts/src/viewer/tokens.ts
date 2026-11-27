/**
 * The viewer's token tables: the data only (no DOM). Every `--bv-*` token has a
 * source in each host (JupyterLab `--jp-*`, VS Code `--vscode-*`) and a light
 * and a dark fallback; the dark fallback follows the landing page tokens
 * (`docs/design/2026-11-27-landing-tokens.css`). With `theme.ts`, the only place
 * with literal colours. The JupyterLab host reads `JP_VARIABLES` to know which
 * `--jp-*` values to send in a `blaym:theme` message.
 */
export interface ThemeToken {
  token: string;
  jp: string;
  vscode: string | null;
  light: string;
  dark: string;
}

export const THEME_TOKENS: readonly ThemeToken[] = [
  {
    token: '--bv-bg',
    jp: '--jp-layout-color0',
    vscode: '--vscode-editor-background',
    light: 'hsl(200 11% 98%)',
    dark: 'hsl(200 11% 9%)'
  },
  {
    token: '--bv-card',
    jp: '--jp-layout-color1',
    vscode: '--vscode-sideBar-background',
    light: 'hsl(0 0% 100%)',
    dark: 'hsl(200 11% 14%)'
  },
  {
    token: '--bv-border',
    jp: '--jp-border-color1',
    vscode: '--vscode-panel-border',
    light: 'hsl(200 9% 78%)',
    dark: 'hsl(200 9% 27%)'
  },
  {
    token: '--bv-fg',
    jp: '--jp-ui-font-color1',
    vscode: '--vscode-foreground',
    light: 'hsl(200 15% 12%)',
    dark: 'hsl(190 15% 97%)'
  },
  {
    token: '--bv-muted',
    jp: '--jp-ui-font-color2',
    vscode: '--vscode-descriptionForeground',
    light: 'hsl(200 10% 36%)',
    dark: 'hsl(199 9% 74%)'
  },
  {
    token: '--bv-primary',
    jp: '--jp-brand-color1',
    vscode: '--vscode-focusBorder',
    light: 'hsl(172 100% 22%)',
    dark: 'hsl(172 100% 50%)'
  },
  {
    token: '--bv-ai',
    jp: '--jp-accent-color1',
    vscode: '--vscode-charts-purple',
    light: '#7a2fc4',
    dark: '#b86afa'
  },
  {
    token: '--bv-danger',
    jp: '--jp-error-color1',
    vscode: '--vscode-errorForeground',
    light: 'hsl(345 87% 38%)',
    dark: 'hsl(345 87% 53%)'
  },
  {
    token: '--bv-warn',
    jp: '--jp-warn-color1',
    vscode: '--vscode-editorWarning-foreground',
    light: 'hsl(30 90% 27%)',
    dark: 'hsl(38 92% 60%)'
  },
  {
    token: '--bv-font-mono',
    jp: '--jp-code-font-family',
    vscode: '--vscode-editor-font-family',
    light: "ui-monospace, 'SFMono-Regular', Menlo, monospace",
    dark: "ui-monospace, 'SFMono-Regular', Menlo, monospace"
  },
  {
    token: '--bv-font-ui',
    jp: '--jp-ui-font-family',
    vscode: '--vscode-font-family',
    light: 'system-ui, -apple-system, sans-serif',
    dark: 'system-ui, -apple-system, sans-serif'
  },
  {
    token: '--bv-font-size',
    jp: '--jp-ui-font-size1',
    vscode: '--vscode-font-size',
    light: '13px',
    dark: '13px'
  },
  {
    token: '--bv-radius',
    jp: '--jp-border-radius',
    vscode: null,
    light: '0.6rem',
    dark: '0.6rem'
  }
];

/**
 * The colour family of each kind of step (`icons.ts` reads these). They have
 * no host source: a host theme does not say what a "run" colour is.
 */
export const KIND_TOKENS: readonly {
  token: string;
  light: string;
  dark: string;
}[] = [
  { token: '--bv-kind-run', light: '#2563c9', dark: '#5b9bff' },
  { token: '--bv-kind-struct', light: '#1f8049', dark: '#4cc380' },
  { token: '--bv-kind-chart', light: '#b86500', dark: '#ffa51f' },
  { token: '--bv-kind-table', light: '#0b7f72', dark: '#2fd0bd' },
  { token: '--bv-kind-restart', light: '#8d5524', dark: '#d19a66' },
  { token: '--bv-kind-outside', light: '#c2185b', dark: '#f06292' },
  { token: '--bv-kind-branch', light: '#455a64', dark: '#90a4ae' }
];

/**
 * The colours of the diff overlay: a cell or line added, removed or changed.
 * Also used by the change bars of the version rows. No host source.
 */
export const DIFF_TOKENS: readonly {
  token: string;
  light: string;
  dark: string;
}[] = [
  { token: '--bv-added', light: '#1f8049', dark: '#4cc380' },
  { token: '--bv-removed', light: '#c62a47', dark: '#f0647f' },
  { token: '--bv-changed', light: '#2563c9', dark: '#5b9bff' }
];

/** The `--jp-*` names a JupyterLab host sends in `blaym:theme`. */
export const JP_VARIABLES: readonly string[] = THEME_TOKENS.map(t => t.jp);
