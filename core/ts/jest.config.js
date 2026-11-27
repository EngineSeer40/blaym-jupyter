const jestJupyterLab = require('@jupyterlab/testutils/lib/jest-config');

const baseConfig = jestJupyterLab(__dirname);

module.exports = {
  ...baseConfig,
  automock: false,
  testRegex: 'src/.*/.*.spec.ts[x]?$',
  transformIgnorePatterns: ['/node_modules/(?!(@jupyterlab/|lib0|yjs).+)']
};
