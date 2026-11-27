module.exports = {
  testEnvironment: 'node',
  testRegex: 'src/__tests__/.*\\.spec\\.ts$',
  transform: {
    '^.+\\.ts$': [
      'ts-jest',
      {
        diagnostics: false,
        tsconfig: {
          isolatedModules: true,
          module: 'commonjs',
          target: 'ES2022',
          esModuleInterop: true,
          resolveJsonModule: true
        }
      }
    ]
  }
};
