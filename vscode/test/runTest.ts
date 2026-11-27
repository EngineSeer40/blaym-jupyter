// End-to-end launcher: VS Code from the S3c probe's portable install, driving the suite in suite/recording.ts.
// Run through test/run.sh only. Phase 1 records the scripted session and ends with VS Code killed;
// phase 2 reopens it, checks the start-up gap and compares every checkpoint with the stored history.
import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

// @vscode/test-electron lives in the probe's node_modules (read-only reference)
const s3c = path.resolve(__dirname, '..', '..', '..', '..', 'probes', 's3c');
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { downloadAndUnzipVSCode, runTests } = require(path.join(s3c, 'probe-ext', 'node_modules', '@vscode', 'test-electron'));

const VSCODE_VERSION = '1.145.3';
const vscodeDir = path.join(s3c, 'vscode');

async function main(): Promise<void> {
  const phase = process.env.BLAYM_E2E_PHASE ?? '1';
  const dir = process.env.BLAYM_E2E_DIR!;
  const extensionDir = path.resolve(__dirname, '..', '..', '..'); // vscode/
  process.env.VSCODE_PORTABLE = path.join(vscodeDir, 'portable');
  const extDir = path.join(vscodeDir, 'extensions');
  const userDir = path.join(vscodeDir, 'portable', 'user-data');
  const settingsDir = path.join(userDir, 'User');
  fs.mkdirSync(settingsDir, { recursive: true });
  const exe: string = await downloadAndUnzipVSCode({ version: VSCODE_VERSION, cachePath: path.join(vscodeDir, 'app') });
  fs.writeFileSync(
    path.join(settingsDir, 'settings.json'),
    JSON.stringify({
      'telemetry.telemetryLevel': 'off',
      'update.mode': 'none',
      'extensions.autoUpdate': false,
      'extensions.autoCheckUpdates': false,
      'workbench.startupEditor': 'none',
      'security.workspace.trust.enabled': false,
      'python.defaultInterpreterPath': path.join(s3c, '.venv-kernel', 'bin', 'python'),
      'python.terminal.activateEnvironment': false,
      'jupyter.askForKernelRestart': false
    })
  );
  // Phase 1 ends by the suite writing `kill-me`; VS Code and the kernel are then killed outright.
  const marker = path.join(dir, 'kill-me');
  const killer = setInterval(() => {
    if (phase === '1' && fs.existsSync(marker)) {
      clearInterval(killer);
      for (const pattern of [path.join(vscodeDir, 'app'), path.join(s3c, '.venv-kernel')]) {
        cp.spawnSync('pkill', ['-9', '-f', pattern]);
      }
    }
  }, 250);
  try {
    await runTests({
      vscodeExecutablePath: exe,
      extensionDevelopmentPath: extensionDir,
      extensionTestsPath: path.join(__dirname, 'suite', 'recording'),
      extensionTestsEnv: {
        BLAYM_E2E_DIR: dir,
        BLAYM_E2E_PHASE: phase,
        BLAYM_EXPORT: process.env.BLAYM_EXPORT ?? '',
        BLAYM_EXPORT_OUT: process.env.BLAYM_EXPORT_OUT ?? '',
        BLAYM_STORE_ROOT: path.join(dir, 'store'),
        S3C_ROOT: s3c
      },
      launchArgs: [path.join(dir, 'workspace'), '--extensions-dir', extDir, '--user-data-dir', userDir, '--disable-workspace-trust', '--disable-gpu']
    });
  } catch (e) {
    if (!(phase === '1' && fs.existsSync(marker))) {
      throw e;
    }
    console.log('PHASE 1 ended by killing VS Code, as planned');
  } finally {
    clearInterval(killer);
  }
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
