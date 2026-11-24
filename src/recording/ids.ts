import type { Ids } from './types';

/**
 * Cell identity during one page open (S5-U). With stable ids (nbformat 4.5 or
 * newer) the editor's id is the Blaym id and nothing is translated. Otherwise a
 * translation table maps the editor's temporary id to the Blaym id where they
 * differ; a cell the page creates keeps its own id as the Blaym id.
 */
export class CellIdentity {
  private readonly toBlaym = new Map<string, string>();
  private readonly blaymIds = new Set<string>();

  constructor(
    readonly stable: boolean,
    private readonly ids: Ids
  ) {}

  /** The Blaym id of an editor id (a new cell gets its own id, or a fresh one on a clash). */
  blaymId(modelId: string): string {
    if (this.stable) {
      return modelId;
    }
    let id = this.toBlaym.get(modelId);
    if (id === undefined) {
      id = this.blaymIds.has(modelId) ? this.ids.uuid() : modelId;
      this.toBlaym.set(modelId, id);
      this.blaymIds.add(id);
    }
    return id;
  }

  /** Record that the editor's id stands for a Blaym id (from alignment). */
  setAlias(modelId: string, blaymId: string): void {
    if (this.stable) {
      return;
    }
    const old = this.toBlaym.get(modelId);
    if (old !== undefined) {
      this.blaymIds.delete(old);
    }
    this.toBlaym.set(modelId, blaymId);
    this.blaymIds.add(blaymId);
  }

  /** Only the entries where the two ids differ: what `register` sends as `cellAliases`. */
  aliases(): Record<string, string> {
    const out: Record<string, string> = {};
    this.toBlaym.forEach((blaym, model) => {
      if (blaym !== model) {
        out[model] = blaym;
      }
    });
    return out;
  }

  /** The editor id of a Blaym id, if this page knows one. */
  modelIdOf(blaymId: string): string | undefined {
    if (this.stable) {
      return blaymId;
    }
    let found: string | undefined;
    this.toBlaym.forEach((b, m) => {
      if (b === blaymId) {
        found = m;
      }
    });
    return found;
  }
}
