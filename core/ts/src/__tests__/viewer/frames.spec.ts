import {
  SETTLE_MS,
  createFrames,
  createSizeMemory,
  typedText
} from '../../viewer/frames';

describe('quick and full frames', () => {
  it('settles after a pause, types the changed tail at the pace, skips ahead when behind and remembers sizes', () => {
    // Settle: release at once, or 250 ms after the last move.
    let due: (() => void) | undefined;
    const seen: boolean[] = [];
    const frames = createFrames({
      setTimeout: fn => {
        due = fn;
        return () => {
          due = undefined;
        };
      }
    });
    frames.subscribe(m => seen.push(m));
    frames.moving();
    frames.moving();
    expect(frames.isMoving()).toBe(true);
    due?.();
    expect(frames.isMoving()).toBe(false);
    frames.moving();
    frames.release();
    expect(seen).toEqual([true, false, true, false]);
    expect(SETTLE_MS).toBe(250);

    // Typing: the common start stays, the tail types in at 120 a second.
    const from = 'df = load()\nplot(df)';
    const to = 'df = load()\nplot(df, by="region")';
    expect(typedText(from, to, 0)).toBe('df = load()\nplot(df');
    expect(typedText(from, to, 100)).toBe('df = load()\nplot(df, by="region');
    expect(typedText(from, to, 5000)).toBe(to);
    // Far behind: 400 ms of typing remain (48 characters), however little time has passed.
    const long = 'x'.repeat(500);
    expect(typedText('', long, 0)).toHaveLength(500 - 48);

    // A placeholder keeps the size last seen, else the kind's default.
    const sizes = createSizeMemory();
    expect(sizes.heightOf('h1', 'chart')).toBe(240);
    sizes.remember('h1', 313);
    expect(sizes.heightOf('h1', 'chart')).toBe(313);
  });
});
