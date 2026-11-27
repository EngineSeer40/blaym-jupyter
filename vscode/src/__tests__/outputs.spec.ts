import { toNbformatOutputs } from '../outputs';

const bytes = (s: string) => new TextEncoder().encode(s);

describe('toNbformatOutputs', () => {
  it('converts each kind of VS Code output to its nbformat shape', () => {
    const png = new Uint8Array([137, 80, 78, 71]);
    const out = toNbformatOutputs([
      {
        items: [
          { mime: 'application/vnd.code.notebook.stdout', data: bytes('a\n') }
        ]
      },
      {
        items: [
          { mime: 'application/vnd.code.notebook.stderr', data: bytes('b') }
        ]
      },
      {
        metadata: {
          originalError: { ename: 'E', evalue: 'v', traceback: ['t'] }
        },
        items: [
          { mime: 'application/vnd.code.notebook.error', data: bytes('{}') }
        ]
      },
      {
        metadata: { outputType: 'execute_result', executionCount: 3 },
        items: [
          { mime: 'text/plain', data: bytes('4') },
          { mime: 'application/vnd.plotly.v1+json', data: bytes('{"x":1}') },
          { mime: 'image/png', data: png }
        ]
      },
      {
        metadata: {
          outputType: 'display_data',
          transient: { display_id: 'd' }
        },
        items: [{ mime: 'text/html', data: bytes('<b>') }]
      }
    ]);
    expect(out).toEqual([
      { output_type: 'stream', name: 'stdout', text: 'a\n' },
      { output_type: 'stream', name: 'stderr', text: 'b' },
      { output_type: 'error', ename: 'E', evalue: 'v', traceback: ['t'] },
      {
        output_type: 'execute_result',
        execution_count: 3,
        metadata: {},
        data: {
          'text/plain': '4',
          'application/vnd.plotly.v1+json': { x: 1 },
          'image/png': 'iVBORw=='
        }
      },
      {
        output_type: 'display_data',
        metadata: { transient: { display_id: 'd' } },
        data: { 'text/html': '<b>' }
      }
    ]);
  });
});
