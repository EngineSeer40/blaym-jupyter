const jestJupyterLab = require('@jupyterlab/testutils/lib/jest-config');

const baseConfig = jestJupyterLab(__dirname);

module.exports = {
  ...baseConfig,
  automock: false,
  // d3 ships ES modules; tests use its bundled build.
  moduleNameMapper: {
    ...baseConfig.moduleNameMapper,
    '^d3$': require('path').join(
      require('path').dirname(require.resolve('d3')),
      'd3.min.js'
    )
  },
  testRegex: 'src/.*/.*.spec.ts[x]?$',
  transformIgnorePatterns: ['/node_modules/(?!(@jupyterlab/|lib0|yjs).+)']
};
