import type { Json } from './types';

/**
 * The metadata policy (S5-Q, owner O5), the TypeScript applier. The policy
 * itself is the data file `fixtures/recorder/metadata-policy.json`, shared with
 * the Python applier; the adapter passes its content in, and the shared cases
 * file (`metadata-cases.json`) is run against both appliers.
 */

export interface MetadataPolicy {
  version?: number;
  maxValueBytes: number;
  oversizeMarker: Record<string, unknown>;
  notebook: LevelRules;
  cell: LevelRules;
}

export interface LevelRules {
  dropKeys?: string[];
  dropNestedKeys?: Record<string, string[]>;
}

export type MetadataLevel = 'notebook' | 'cell';

export interface PolicyResult {
  metadata: Record<string, unknown>;
  /** Sorted; a nested key is `outer.inner`. */
  dropped: string[];
}

/** UTF-8 length of a string (no TextEncoder in the neutral library). */
export function utf8Length(text: string): number {
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c < 0x80) {
      bytes += 1;
    } else if (c < 0x800) {
      bytes += 2;
    } else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) {
      const d = text.charCodeAt(i + 1);
      if (d >= 0xdc00 && d <= 0xdfff) {
        bytes += 4;
        i++;
      } else {
        bytes += 3;
      }
    } else {
      bytes += 3;
    }
  }
  return bytes;
}

export function deepClone<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map(deepClone) as unknown as T;
  }
  if (value !== null && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    Object.keys(value as Record<string, unknown>).forEach(k => {
      out[k] = deepClone((value as Record<string, unknown>)[k]);
    });
    return out as T;
  }
  return value;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

/** Apply the policy. The input is never changed. */
export function applyPolicy(
  metadata: Record<string, unknown> | null | undefined,
  level: MetadataLevel,
  policy: MetadataPolicy
): PolicyResult {
  if (level !== 'notebook' && level !== 'cell') {
    throw new Error(`unknown metadata level ${String(level)}`);
  }
  const rules = policy[level];
  const result = deepClone(metadata ?? {}) as Record<string, unknown>;
  const dropped: string[] = [];

  (rules.dropKeys ?? []).forEach(key => {
    if (key in result) {
      delete result[key];
      dropped.push(key);
    }
  });

  const nested = rules.dropNestedKeys ?? {};
  Object.keys(nested).forEach(key => {
    const value = result[key];
    if (!isPlainObject(value)) {
      return;
    }
    nested[key].forEach(name => {
      if (name in value) {
        delete value[name];
        dropped.push(`${key}.${name}`);
      }
    });
    if (Object.keys(value).length === 0) {
      delete result[key];
    }
  });

  Object.keys(result).forEach(key => {
    const size = utf8Length(JSON.stringify(result[key] as Json));
    if (size > policy.maxValueBytes) {
      result[key] = { ...policy.oversizeMarker, bytes: size };
      dropped.push(key);
    }
  });

  return { metadata: result, dropped: dropped.sort() };
}

/** JSON text with sorted keys, to compare metadata regardless of key order. */
export function stableStringify(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableStringify).join(',')}]`;
  }
  if (isPlainObject(value)) {
    return `{${Object.keys(value)
      .sort()
      .map(k => `${JSON.stringify(k)}:${stableStringify(value[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}
