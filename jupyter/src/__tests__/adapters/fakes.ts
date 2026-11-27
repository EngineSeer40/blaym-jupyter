import type { NotebookRegistration, NotebookStatus } from 'blaym-notebook-types';
import type { TabElection } from 'blaym-notebook-core';
import type {
  KernelLike,
  PanelLike,
  SharedCellLike,
  SharedNotebookLike
} from '../../adapters/jupyterlab/observer';
import {
  FakeTime,
  FakeTransport,
  POLICY,
  SeqIds,
  settle,
  status
} from '../../../../core/ts/src/__tests__/recording/generate';

export { FakeTime, POLICY, settle, status };

export class FakeSignal<A = unknown> {
  slots = new Set<(s: unknown, a: A) => void>();
  connect(slot: (s: unknown, a: A) => void): boolean {
    this.slots.add(slot);
    return true;
  }
  disconnect(slot: (s: unknown, a: A) => void): boolean {
    return this.slots.delete(slot);
  }
  emit(a?: A): void {
    [...this.slots].forEach(s => s(this, a as A));
  }
}

export class FakeCell implements SharedCellLike {
  changed = new FakeSignal();
  constructor(
    public id: string,
    public cell_type: string,
    public source: string,
    public outputs: Record<string, unknown>[] = []
  ) {}
  getId(): string {
    return this.id;
  }
  getSource(): string {
    return this.source;
  }
  getMetadata(): Record<string, unknown> {
    return {};
  }
  getOutputs(): Record<string, unknown>[] {
    return this.outputs;
  }
  type(source: string): void {
    this.source = source;
    this.changed.emit();
  }
}

export class FakeShared implements SharedNotebookLike {
  changed = new FakeSignal();
  constructor(
    public cells: FakeCell[],
    public nbformat = 4,
    public nbformat_minor = 5
  ) {}
  getMetadata(): Record<string, unknown> {
    return {
      kernelspec: { name: 'python3' },
      language_info: { name: 'python' }
    };
  }
  transact(f: () => void): void {
    f();
  }
  insert(i: number, c: FakeCell): void {
    this.cells.splice(i, 0, c);
    this.changed.emit();
  }
}

export class FakeKernel implements KernelLike {
  name = 'python3';
  anyMessage = new FakeSignal<any>();
  send(type: string, cellId?: string, msgId = `m-${Math.random()}`): void {
    this.anyMessage.emit({
      direction: 'send',
      msg: { header: { msg_type: type, msg_id: msgId }, metadata: { cellId } }
    });
  }
}

export class FakePanel implements PanelLike {
  saveState = new FakeSignal<string>();
  disposed = new FakeSignal();
  kernelChanged = new FakeSignal();
  connectionStatusChanged = new FakeSignal();
  session: { kernel: FakeKernel | null } = { kernel: new FakeKernel() };
  model: { sharedModel: FakeShared };
  path = 'a.ipynb';
  ready = Promise.resolve();
  context: any;
  constructor(shared: FakeShared) {
    this.model = { sharedModel: shared };
    this.context = this;
    this.context.sessionContext = this;
  }
  get kernel(): FakeKernel | null {
    return this.session.kernel;
  }
}

export class CapturingTransport extends FakeTransport {
  registrations: NotebookRegistration[] = [];
  tipCells: Array<{ cellId: string; type: any; code: string }> = [];
  register(req?: NotebookRegistration): Promise<NotebookStatus> {
    if (req) {
      this.registrations.push(req);
    }
    return super.register();
  }
  tip(): Promise<Array<{ cellId: string; type: any; code: string }>> {
    return Promise.resolve(this.tipCells);
  }
}

export class FakeElection implements TabElection {
  acquired: string[] = [];
  constructor(public result: 'active' | 'inactive' = 'active') {}
  acquire(id: string): Promise<'active' | 'inactive'> {
    this.acquired.push(id);
    return Promise.resolve(this.result);
  }
  onLost(): () => void {
    return () => undefined;
  }
}

export class UuidIds extends SeqIds {
  constructor() {
    super('u');
  }
}
