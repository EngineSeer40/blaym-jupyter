// Bundles src/viewer/main.ts into viewer.js and viewer.css.
// Usage: node scripts/build-viewer.js [outDir]   (default: the shipped viewer_static folder)
const path = require('path');
const esbuild = require('esbuild');

const outDir = path.resolve(
  process.argv[2] ||
    path.join(
      __dirname,
      '..',
      '..',
      'python',
      'blaym_notebook',
      'viewer_static'
    )
);
esbuild
  .build({
    entryPoints: {
      viewer: path.join(__dirname, '..', 'src', 'viewer', 'main.ts')
    },
    outdir: outDir,
    bundle: true,
    minify: true,
    sourcemap: false,
    format: 'iife',
    target: 'es2018',
    legalComments: 'none',
    logLevel: 'warning'
  })
  .catch(() => process.exit(1));
