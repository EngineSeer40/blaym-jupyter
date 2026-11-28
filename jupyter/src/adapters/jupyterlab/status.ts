import type { NotebookStatus } from 'blaym-notebook-types';
import type { ObserverState } from './observer';

export type Severity = 'info' | 'warning' | 'error';

/** Shows a short line to the user (`Notification.manager` in the page). */
export interface Notifier {
  notify(severity: Severity, text: string): void;
}

export interface Described {
  /** For `data-blaym-state` and styling. */
  key: string;
  label: string;
  tooltip: string;
  /** Recording is held by another tab or editor and this tab can take it. */
  recordHere?: boolean;
  /** The same line without the action words (the viewer shows its own button). */
  line?: string;
}

const PROBLEMS: Record<string, string> = {
  unauthorized: 'the sign-in for the server expired; reload the page',
  snapshot_refused: 'the server refused the notebook state'
};

function reason(detail: string): string {
  return PROBLEMS[detail] ?? detail;
}

/** Plain words for each state (the status bar item and the toolbar toggle). */
export function describeState(state: ObserverState): Described {
  switch (state.kind) {
    case 'starting':
      return { key: 'starting', label: 'Blaym: starting', tooltip: '' };
    case 'recording':
      return {
        key: 'recording',
        label: 'Blaym: recording',
        tooltip: 'Click to switch recording off for this notebook'
      };
    case 'off':
      return {
        key: 'off',
        label: 'Blaym: off',
        tooltip: 'Click to switch recording on for this notebook'
      };
    case 'inactive':
      if (state.reason === 'other_tab') {
        return {
          key: 'other-tab',
          label:
            'Blaym: recording in another tab of this browser · record here',
          tooltip:
            'Recording stays with the other tab until it closes. Click to record in this tab instead.',
          recordHere: true,
          line: 'Blaym: recording in another tab of this browser'
        };
      }
      if (state.reason === 'other_recorder') {
        return {
          key: 'other-recorder',
          label: 'Blaym: recording in another tab or window · record here',
          tooltip:
            'Recording stays with the other tab until it closes, or about a minute after it stops answering.',
          recordHere: true,
          line: 'Blaym: recording in another tab or window'
        };
      }
      return {
        key: 'collaboration',
        label: 'Blaym: off (collaboration mode)',
        tooltip: 'Collaboration mode is not supported yet'
      };
    case 'problem': {
      const paused = state.detail in PROBLEMS;
      return {
        key: paused ? 'paused' : 'problem',
        label: `Blaym: ${paused ? 'paused' : 'problem'} (${reason(state.detail)})`,
        tooltip: state.detail
      };
    }
  }
}

/** Kernel statuses that mean a restart. */
const RESTARTS = new Set(['restarting', 'autorestarting']);

/**
 * One notebook's status and its few notifications (quiet edges): a
 * message is shown when a state is entered, never for normal events.
 */
export class NotebookPresenter {
  private shown?: Described;
  private lastKey?: string;
  private readonly noticed = new Set<string>();
  private restarting = false;

  constructor(
    private readonly notifier: Notifier,
    private readonly onChange: (d: Described) => void
  ) {}

  get described(): Described | undefined {
    return this.shown;
  }

  onState(state: ObserverState): void {
    const d = describeState(state);
    this.shown = d;
    this.onChange(d);
    if (d.key === this.lastKey) {
      return;
    }
    this.lastKey = d.key;
    switch (state.kind) {
      case 'inactive':
        if (state.reason === 'other_tab') {
          this.notifier.notify(
            'info',
            'Another tab is recording this notebook. This tab records nothing.'
          );
        } else if (state.reason === 'other_recorder') {
          this.notifier.notify(
            'info',
            'Another editor is recording this notebook.'
          );
        } else {
          this.notifier.notify(
            'info',
            'Collaboration mode is not supported yet. Recording stays off.'
          );
        }
        break;
      case 'problem':
        this.notifier.notify(
          'error',
          `Blaym stopped recording: ${reason(state.detail)}.`
        );
        break;
    }
  }

  /** Quiet notices about how the notebook was identified (once each). */
  onStatus(status: NotebookStatus): void {
    for (const n of status.notices) {
      if (this.noticed.has(n.code)) {
        continue;
      }
      this.noticed.add(n.code);
      this.notifier.notify(
        'info',
        n.code === 'moved'
          ? 'This notebook looks like it was moved. Its history continues.'
          : 'This notebook looks like a copy. It starts its own history.'
      );
    }
  }

  /** From the session context's status: one short line per restart. */
  onKernelStatus(status: string): void {
    const now = RESTARTS.has(status);
    if (now && !this.restarting) {
      this.notifier.notify('info', 'Kernel restarted.');
    }
    this.restarting = now;
  }
}

/** Whether the toggle should switch on (true), off (false) or do nothing (undefined). */
export function toggleTarget(state: ObserverState): boolean | undefined {
  if (state.kind === 'off') {
    return true;
  }
  if (state.kind === 'recording') {
    return false;
  }
  return undefined;
}
