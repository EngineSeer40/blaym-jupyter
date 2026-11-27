import type * as vscode from 'vscode';
import type { ServiceTransport } from './transport';

/** Anything to close on deactivate: a session or a run tracker. */
export interface Closeable {
  close(): unknown;
}

/**
 * What the extension's modules share. Module-internal on purpose: `activate`
 * returns nothing, because other extensions can read its return value and the
 * transport is signed in.
 */
export const blaym: {
  transport?: ServiceTransport;
  /** Whether recording is on for this notebook (the toggle command). */
  isRecording: (uri: vscode.Uri) => boolean;
  /** Closed on deactivate: trackers first, then sessions. */
  closers: { tracker: Set<Closeable>; session: Set<Closeable> };
} = {
  isRecording: () => false,
  closers: { tracker: new Set(), session: new Set() }
};
