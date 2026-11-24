import type { ObserverSettings } from './observer';

/** The Blaym settings (`schema/plugin.json`), with the defaults of the plan (S5-O, O8). */
export interface BlaymSettings {
  typingPauseMs: number;
  typingMaxMs: number;
  aiWindowMs: number;
  heartbeatMs: number;
  retryFirstMs: number;
  retryMaxMs: number;
  runWaitMs: number;
  runWaitAliveMs: number;
}

export const DEFAULT_SETTINGS: BlaymSettings = {
  typingPauseMs: 800,
  typingMaxMs: 5000,
  aiWindowMs: 250,
  heartbeatMs: 15000,
  retryFirstMs: 500,
  retryMaxMs: 8000,
  runWaitMs: 5000,
  runWaitAliveMs: 30000
};

/** Settings that are waits or intervals: 0 would loop tightly, so it falls back to the default. */
const INTERVALS = new Set<keyof BlaymSettings>([
  'typingPauseMs',
  'typingMaxMs',
  'heartbeatMs',
  'retryFirstMs',
  'retryMaxMs',
  'runWaitMs',
  'runWaitAliveMs'
]);

/** The part of a setting registry entry that is read. */
export interface SettingsLike {
  composite: Record<string, unknown>;
  changed: {
    connect(slot: () => void): unknown;
  };
}

/** Settings read live: every read sees the latest values. */
export class LiveSettings {
  private values: BlaymSettings = { ...DEFAULT_SETTINGS };

  constructor(source?: SettingsLike) {
    if (source) {
      this.load(source.composite);
      source.changed.connect(() => this.load(source.composite));
    }
  }

  get current(): BlaymSettings {
    return this.values;
  }

  /** What the observer reads (getters, so a change is seen the next time it is read). */
  get observer(): ObserverSettings {
    const live = (): BlaymSettings => this.values;
    return {
      get pauseMs(): number {
        return live().typingPauseMs;
      },
      get maxMs(): number {
        return live().typingMaxMs;
      },
      get heartbeatMs(): number {
        return live().heartbeatMs;
      }
    };
  }

  private load(composite: Record<string, unknown>): void {
    const next = { ...DEFAULT_SETTINGS };
    (Object.keys(next) as Array<keyof BlaymSettings>).forEach(key => {
      const v = composite[key];
      const min = INTERVALS.has(key) ? 1 : 0;
      if (typeof v === 'number' && Number.isFinite(v) && v >= min) {
        next[key] = v;
      }
    });
    this.values = next;
  }
}
