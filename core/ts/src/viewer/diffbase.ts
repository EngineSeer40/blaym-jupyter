/**
 * What the diff compares against: which tick the base is, and the base state
 * read for display. The read goes through `client.state` and is cached by
 * `(branch, tick)`; it is not a navigator position, so it never moves the view.
 */
import type { RebuiltState } from 'blaym-notebook-types';
import type { IView } from '../navigation';
import type { Client } from './client';
import type { ContextEntry } from './context';
import type { DiffBase } from './focus';
import { versions } from './modes';
import type { VersionMode } from './modes';

/** The tick to compare against, or undefined when there is nothing to compare (following now, none, no earlier version). */
export function baseTick(
  base: DiffBase,
  view: Pick<IView, 'mode' | 'tick' | 'lookingBackFromTip'>,
  entries: readonly ContextEntry[],
  mode: VersionMode
): number | undefined {
  if (base === 'none' || view.mode !== 'lookingBack') {
    return undefined;
  }
  if (typeof base === 'object') {
    return base.tick;
  }
  if (base === 'now') {
    return view.lookingBackFromTip;
  }
  if (view.tick === undefined) {
    return undefined;
  }
  const items = versions(entries, { mode });
  let at = -1;
  items.forEach((item, i) => {
    if (item.firstTick <= (view.tick as number)) {
      at = i;
    }
  });
  return at > 0 ? items[at - 1].lastTick : undefined;
}

export interface BaseCache {
  /** The cached state, if read already. */
  peek(branchId: string, tick: number): RebuiltState | undefined;
  /** Reads it (once) and resolves to it; undefined when it could not be built. */
  load(branchId: string, tick: number): Promise<RebuiltState | undefined>;
}

const MAX_CACHED = 8;

export function createBaseCache(
  client: Pick<Client, 'state'>,
  explorationId: string
): BaseCache {
  const states = new Map<string, RebuiltState>();
  const loading = new Map<string, Promise<RebuiltState | undefined>>();
  const key = (b: string, t: number) => `${b}:${t}`;
  return {
    peek: (b, t) => states.get(key(b, t)),
    load(branchId, tick) {
      const k = key(branchId, tick);
      const have = states.get(k);
      if (have) {
        return Promise.resolve(have);
      }
      let p = loading.get(k);
      if (!p) {
        p = client
          .state({ explorationId, branchId, tick })
          .then(result => {
            if (result.status !== 'ok') {
              return undefined;
            }
            states.set(k, result.state);
            if (states.size > MAX_CACHED) {
              states.delete(states.keys().next().value as string);
            }
            return result.state;
          })
          .catch(() => undefined)
          .finally(() => loading.delete(k));
        loading.set(k, p);
      }
      return p;
    }
  };
}
