import * as cp from 'child_process';
import * as crypto from 'crypto';

/** The service is running: where to reach it and the token it wants. */
export interface RunningService {
  ok: true;
  port: number;
  token: string;
  /** Closing standard input is how the service is told to end. */
  stop(): Promise<void>;
}

/** The service did not start. `storeInUse`: another VS Code window holds the store. */
export interface FailedService {
  ok: false;
  reason: string;
  storeInUse: boolean;
}

export type ServiceStart = RunningService | FailedService;

const READY_LIMIT_MS = 20000;
const STOP_LIMIT_MS = 4000;

/**
 * Starts `python -m blaym_vscode --root <root>` and waits for its ready line.
 * The token goes in on standard input (never in the command line or the
 * environment) and is never logged. Always settles: a failure, an early exit and
 * a silent service all come back as a reason.
 */
export async function startService(o: {
  python: string;
  root: string;
}): Promise<ServiceStart> {
  const token = crypto.randomBytes(32).toString('hex');
  const proc = cp.spawn(o.python, ['-m', 'blaym_vscode', '--root', o.root], {
    stdio: ['pipe', 'pipe', 'ignore'],
    env: { PATH: process.env.PATH ?? '', PYTHONNOUSERSITE: '1' }
  });
  const line = await new Promise<string | undefined>(resolve => {
    let out = '';
    const done = (value: string | undefined) => {
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => done(undefined), READY_LIMIT_MS);
    const firstLine = () =>
      out.includes('\n') ? out.split('\n')[0] : undefined;
    proc.stdout!.on('data', d => {
      out += d;
      const l = firstLine();
      if (l !== undefined) {
        done(l);
      }
    });
    proc.once('error', () => done(undefined));
    proc.once('exit', () => done(firstLine()));
    proc.stdin!.on('error', () => undefined);
    proc.stdin!.write(token + '\n');
  });
  let ready: { ready?: boolean; port?: number; error?: string } | undefined;
  try {
    ready = line ? JSON.parse(line) : undefined;
  } catch {
    ready = undefined;
  }
  if (ready?.ready && typeof ready.port === 'number') {
    return {
      ok: true,
      port: ready.port,
      token,
      stop: () => stop(proc)
    };
  }
  await stop(proc);
  const error = typeof ready?.error === 'string' ? ready.error : undefined;
  return {
    ok: false,
    reason:
      error ??
      (line
        ? 'unreadable answer from the service'
        : 'the service did not start'),
    storeInUse: error === 'store_in_use'
  };
}

function stop(proc: cp.ChildProcess): Promise<void> {
  return new Promise(resolve => {
    if (proc.exitCode !== null || proc.signalCode !== null) {
      resolve();
      return;
    }
    const kill = setTimeout(() => proc.kill('SIGKILL'), STOP_LIMIT_MS);
    proc.once('exit', () => {
      clearTimeout(kill);
      resolve();
    });
    proc.stdin?.end();
  });
}
