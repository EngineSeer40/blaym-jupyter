/**
 * Playwright config for ui-tests/run-galata.sh: it talks to a server the script
 * started (no webServer) and uses the cached headless Chromium.
 * Env (set by the script): BLAYM_PORT, BLAYM_TOKEN, BLAYM_CHROMIUM.
 */
const path = require('path');
const baseConfig = require('@jupyterlab/galata/lib/playwright-config');

module.exports = {
  ...baseConfig,
  testDir: path.join(__dirname, 'tests'),
  testMatch: process.env.BLAYM_SPEC
    ? new RegExp(process.env.BLAYM_SPEC)
    : /recording\.spec\.ts/,
  reporter: [['list']],
  workers: 1,
  retries: 0,
  outputDir: process.env.BLAYM_RESULTS || path.join(__dirname, 'test-results'),
  webServer: undefined,
  use: {
    ...baseConfig.use,
    baseURL: `http://127.0.0.1:${process.env.BLAYM_PORT}`,
    // the token header applies to the page, API and websocket requests
    extraHTTPHeaders: { Authorization: `token ${process.env.BLAYM_TOKEN}` },
    trace: 'off',
    video: 'off',
    launchOptions: { executablePath: process.env.BLAYM_CHROMIUM }
  }
};
