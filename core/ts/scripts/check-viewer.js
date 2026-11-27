// Rebuilds into a temp folder and fails if viewer.js or viewer.css differ from the committed files.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const shipped = path.join(
  __dirname,
  '..',
  '..',
  'python',
  'blaym_notebook',
  'viewer_static'
);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'blaym-viewer-'));
try {
  execFileSync('node', [path.join(__dirname, 'build-viewer.js'), tmp], {
    stdio: 'inherit'
  });
  const stale = ['viewer.js', 'viewer.css'].filter(name => {
    const a = path.join(shipped, name);
    return (
      !fs.existsSync(a) ||
      !fs.readFileSync(a).equals(fs.readFileSync(path.join(tmp, name)))
    );
  });
  if (stale.length) {
    console.error(
      'Stale viewer build: ' + stale.join(', ') + '. Run: npm run build:viewer'
    );
    process.exit(1);
  }
  console.log('viewer build is current');
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
