/**
 * The "changes" view: the version list's rows (same items, windowing and
 * selection, from `versions.ts`) with a small strip on each row that stands for
 * the notebook's cells, top to bottom, drawn left to right. A tick marks where
 * a cell was added (green), removed (red) or changed (blue); a grouped row
 * shows all of its ticks. Runs and outputs get none, and without cell positions
 * in the index no strip is drawn. Colours come from the `bv-tick-*` classes.
 */
import type { Session } from './session';
import { createVersions } from './versions';
import type { VersionList } from './versions';
import type { VersionItem, VersionMode } from './modes';
import type { CellNamer } from './cellname';

const SVG_NS = 'http://www.w3.org/2000/svg';
export const STRIP_WIDTH = 60;
const STRIP_HEIGHT = 28;
const TICK_WIDTH = 3;

function svg(tag: string, attrs: Record<string, string>): SVGElement {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    e.setAttribute(k, v);
  }
  return e;
}

/** The strip of one item: the grey bar and a tick for each of its marks. */
export function changeStrip(item: VersionItem): SVGElement {
  const strip = svg('svg', {
    class: 'bv-strip',
    width: String(STRIP_WIDTH),
    height: String(STRIP_HEIGHT),
    viewBox: `0 0 ${STRIP_WIDTH} ${STRIP_HEIGHT}`,
    'aria-hidden': 'true'
  });
  strip.append(
    svg('rect', {
      class: 'bv-strip-bg',
      width: String(STRIP_WIDTH),
      height: String(STRIP_HEIGHT),
      rx: '3'
    })
  );
  for (const mark of item.marks) {
    const x = Math.min(
      STRIP_WIDTH - TICK_WIDTH,
      Math.max(0, mark.at * STRIP_WIDTH - TICK_WIDTH / 2)
    );
    strip.append(
      svg('rect', {
        class: `bv-tick bv-tick-${mark.tone}`,
        x: x.toFixed(1),
        y: '2',
        width: String(TICK_WIDTH),
        height: String(STRIP_HEIGHT - 4)
      })
    );
  }
  return strip;
}

export function createChanges(
  target: HTMLElement,
  session: Session,
  initialMode: VersionMode = 'grouped',
  cellName?: CellNamer
): VersionList {
  let positioned = false;
  const list = createVersions(target, session, initialMode, {
    title: 'changes',
    cellName,
    decorate: item => (positioned ? changeStrip(item) : null)
  });
  return {
    setEntries(entries) {
      // An answer without cell positions draws no strips.
      positioned = entries.some(e => e.cellIndex !== undefined);
      list.setEntries(entries);
    },
    refresh: list.refresh
  };
}
