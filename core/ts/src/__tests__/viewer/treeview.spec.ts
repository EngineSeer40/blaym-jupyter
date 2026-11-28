import {
  EDGE,
  alongOf,
  clampOff,
  contentLength,
  followOff,
  offForAnchor,
  posOf
} from '../../viewer/treeview';
import type { Viewport } from '../../viewer/treeview';

describe('treeview mapping', () => {
  const base = { size: 101, step: 12, length: 300, pad: 20 };

  it('round-trips both directions, clamps both ends, follows inside the margins and keeps the anchor', () => {
    for (const up of [true, false]) {
      const v: Viewport = { ...base, up };
      for (const pos of [0, 37, 100]) {
        expect(posOf(v, alongOf(v, pos))).toBeCloseTo(pos, 9);
      }
      // Newest at the start when up, oldest when not.
      expect(alongOf(v, up ? 100 : 0)).toBe(v.pad);
      // Clamp at both ends.
      expect(clampOff(v, -50)).toBe(0);
      expect(clampOff(v, 1e9)).toBe(contentLength(v) - v.length);
      // Follow: a position in the middle stays; near an edge it lands on the inner margin; off screen it jumps.
      expect(followOff(v, 100, posOf(v, 100 + 150)).off).toBe(100);
      const near = followOff(v, 100, posOf(v, 100 + 5));
      expect(near.jump).toBe(false);
      expect(alongOf(v, posOf(v, 100 + 5)) - near.off).toBeCloseTo(
        EDGE * v.length,
        9
      );
      expect(followOff(v, 0, up ? 0 : 100).jump).toBe(true);
      // Zoom anchor: the position under the pointer stays under it.
      const pos = posOf(v, 400 + 90);
      const zoomed = { ...v, step: 30 };
      const off = offForAnchor(zoomed, pos, 90);
      expect(alongOf(zoomed, pos) - off).toBeCloseTo(90, 9);
    }
  });
});
