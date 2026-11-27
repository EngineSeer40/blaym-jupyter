/**
 * @jest-environment jsdom
 */
import { THEME_TOKENS, startTheme } from '../../viewer/theme';
import type { HostMessageEvent, HostPort } from '../../viewer/host';

function setup() {
  let dark = false;
  const mediaListeners: (() => void)[] = [];
  (window as any).matchMedia = () => ({
    get matches() {
      return dark;
    },
    addEventListener: (_: string, fn: () => void) => mediaListeners.push(fn),
    removeEventListener: () => undefined
  });
  let hostFn: ((ev: HostMessageEvent) => void) | undefined;
  const port: HostPort = {
    post: () => undefined,
    listen: fn => {
      hostFn = fn;
      return () => undefined;
    }
  };
  const stop = startTheme(window, port);
  return {
    stop,
    send: (data: unknown) => hostFn!({ data, origin: '', source: null }),
    setDark: (v: boolean) => {
      dark = v;
      mediaListeners.forEach(f => f());
    }
  };
}

const root = () => document.documentElement;
const attr = () => root().getAttribute('data-bv-theme');
const bg = () => root().style.getPropertyValue('--bv-bg');
const tok = (name: string) => THEME_TOKENS.find(t => t.token === name)!;

it('follows host message, then VS Code class, then prefers-color-scheme', async () => {
  const t = setup();
  // (c) system setting
  expect(attr()).toBe('light');
  t.setDark(true);
  expect(attr()).toBe('dark');
  expect(bg()).toBe(tok('--bv-bg').dark);

  // (a) host message wins; a missing value uses the kind's fallback
  t.send({
    type: 'blaym:theme',
    kind: 'light',
    values: { '--jp-layout-color0': '#fafafa' }
  });
  expect(attr()).toBe('light');
  expect(bg()).toBe('#fafafa');
  expect(root().style.getPropertyValue('--bv-fg')).toBe(tok('--bv-fg').light);

  // host message gone is not possible: a later message replaces it
  t.send({ type: 'blaym:theme', kind: 'dark' });
  expect(attr()).toBe('dark');
  expect(bg()).toBe(tok('--bv-bg').dark);
  t.stop();

  // (b) VS Code class, watched; no host message
  document.documentElement.removeAttribute('style');
  const v = setup();
  document.body.className = 'vscode-dark';
  await Promise.resolve();
  expect(attr()).toBe('dark');
  document.body.className = 'vscode-light';
  await Promise.resolve();
  expect(attr()).toBe('light');
  expect(bg()).toBe(tok('--bv-bg').light);
  v.stop();
});
