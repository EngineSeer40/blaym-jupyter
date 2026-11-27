/** One output item of VS Code's notebook model. */
export interface OutputItemLike {
  mime: string;
  data: Uint8Array;
}

/** One output of a cell as VS Code's notebook model shows it. */
export interface OutputLike {
  metadata?: Record<string, unknown>;
  items: OutputItemLike[];
}

const STDOUT = 'application/vnd.code.notebook.stdout';
const STDERR = 'application/vnd.code.notebook.stderr';
const ERROR = 'application/vnd.code.notebook.error';
const BINARY = /^(image\/(?!svg)|audio\/|video\/|application\/pdf$)/;
const JSON_MIME = /^application\/(.+\+)?json$/;

const text = (data: Uint8Array): string => new TextDecoder().decode(data);

/** The nbformat value of one MIME item: text, base64 for binary, parsed JSON for JSON. */
function mimeValue(item: OutputItemLike): unknown {
  if (BINARY.test(item.mime)) {
    return Buffer.from(item.data).toString('base64');
  }
  const s = text(item.data);
  if (JSON_MIME.test(item.mime)) {
    try {
      return JSON.parse(s);
    } catch {
      return s;
    }
  }
  return s;
}

/** VS Code's outputs of a cell as raw nbformat outputs. */
export function toNbformatOutputs(
  outputs: readonly OutputLike[]
): Record<string, unknown>[] {
  return outputs.map(o => {
    const stream = o.items.find(i => i.mime === STDOUT || i.mime === STDERR);
    if (stream) {
      return {
        output_type: 'stream',
        name: stream.mime === STDOUT ? 'stdout' : 'stderr',
        text: text(stream.data)
      };
    }
    const meta = o.metadata ?? {};
    const error = o.items.find(i => i.mime === ERROR);
    if (error) {
      const original = meta.originalError as
        Record<string, unknown> | undefined;
      if (original) {
        return { ...original, output_type: 'error' };
      }
      const e = JSON.parse(text(error.data)) as {
        name?: string;
        message?: string;
        stack?: string;
      };
      return {
        output_type: 'error',
        ename: e.name ?? '',
        evalue: e.message ?? '',
        traceback: (e.stack ?? '').split('\n')
      };
    }
    const data: Record<string, unknown> = {};
    o.items.forEach(i => (data[i.mime] = mimeValue(i)));
    const transient = meta.transient as { display_id?: string } | undefined;
    const base = {
      data,
      metadata: transient?.display_id
        ? { transient: { display_id: transient.display_id } }
        : {}
    };
    return meta.outputType === 'execute_result'
      ? {
          ...base,
          output_type: 'execute_result',
          execution_count: meta.executionCount ?? null
        }
      : { ...base, output_type: 'display_data' };
  });
}
