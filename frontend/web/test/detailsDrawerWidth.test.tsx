import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  DRAWER_WIDTH_DEFAULT,
  DRAWER_WIDTH_MIN,
  DRAWER_WIDTH_STORAGE_KEY,
  clampDrawerWidth,
  drawerWidthMaxFor,
  nextToggleDrawerWidth,
  readDrawerWidth,
  writeDrawerWidth,
} from '../src/drawerWidth';

// LHH-DETAILS-PANEL-a: behaviour for the right-hand Details slide-over width.
// The component lives inside `App.tsx` and is reachable only after a stack of
// hooks, but the actual decisions are pure functions of (value, viewport) and
// storage — so they are tested directly. The CSS uses `var(--drawer-width,
// 410px)` as a fallback, so the default value here has to match that fallback
// (the constraint is verified by the type/style assertions in the App.tsx JSX
// and style.css).

const NARROW_VIEWPORT = 1700; // max = floor(0.9 * 1700) = 1530
const NARROW_MAX = Math.floor(0.9 * NARROW_VIEWPORT); // 1530
const WIDE_VIEWPORT = 2400; // max = 2160
const WIDE_MAX = Math.floor(0.9 * WIDE_VIEWPORT); // 2160

test('clamp: anything below the 410 px minimum snaps up to 410', () => {
  assert.equal(clampDrawerWidth(0, NARROW_VIEWPORT), DRAWER_WIDTH_MIN);
  assert.equal(clampDrawerWidth(120, NARROW_VIEWPORT), DRAWER_WIDTH_MIN);
  assert.equal(clampDrawerWidth(409, NARROW_VIEWPORT), DRAWER_WIDTH_MIN);
  assert.equal(clampDrawerWidth(-50, NARROW_VIEWPORT), DRAWER_WIDTH_MIN);
});

test('clamp: any value above 90% of the viewport snaps down to 90%', () => {
  // 1700 px viewport -> 90% = 1530 px
  assert.equal(clampDrawerWidth(NARROW_MAX + 1, NARROW_VIEWPORT), NARROW_MAX);
  assert.equal(clampDrawerWidth(99999, NARROW_VIEWPORT), NARROW_MAX);
  // 2400 px viewport -> 90% = 2160 px
  assert.equal(clampDrawerWidth(WIDE_MAX + 1, WIDE_VIEWPORT), WIDE_MAX);
  assert.equal(clampDrawerWidth(100000, WIDE_VIEWPORT), WIDE_MAX);
});

test('clamp: values inside [min, max] pass through, rounded', () => {
  assert.equal(clampDrawerWidth(410, NARROW_VIEWPORT), DRAWER_WIDTH_MIN);
  assert.equal(clampDrawerWidth(411, NARROW_VIEWPORT), 411);
  assert.equal(clampDrawerWidth(800, NARROW_VIEWPORT), 800);
  assert.equal(clampDrawerWidth(NARROW_MAX, NARROW_VIEWPORT), NARROW_MAX);
  assert.equal(clampDrawerWidth(1234.6, NARROW_VIEWPORT), 1235);
});

test('clamp: non-finite input falls back to the minimum', () => {
  assert.equal(clampDrawerWidth(NaN, NARROW_VIEWPORT), DRAWER_WIDTH_MIN);
  assert.equal(clampDrawerWidth(Number.POSITIVE_INFINITY, NARROW_VIEWPORT), DRAWER_WIDTH_MIN);
});

test('drawerWidthMaxFor reports floor(0.9 * viewport) but never below min', () => {
  assert.equal(drawerWidthMaxFor(NARROW_VIEWPORT), NARROW_MAX);
  assert.equal(drawerWidthMaxFor(WIDE_VIEWPORT), WIDE_MAX);
  // Tiny viewports cannot go below the minimum.
  assert.equal(drawerWidthMaxFor(100), DRAWER_WIDTH_MIN);
});

test('toggle: from the default 410, one click expands to 90% of the viewport', () => {
  assert.equal(nextToggleDrawerWidth(DRAWER_WIDTH_DEFAULT, NARROW_VIEWPORT), NARROW_MAX);
  assert.equal(nextToggleDrawerWidth(DRAWER_WIDTH_DEFAULT, WIDE_VIEWPORT), WIDE_MAX);
});

test('toggle: once the drawer is at the live max, the next click collapses back to 410', () => {
  assert.equal(nextToggleDrawerWidth(NARROW_MAX, NARROW_VIEWPORT), DRAWER_WIDTH_MIN);
  assert.equal(nextToggleDrawerWidth(WIDE_MAX, WIDE_VIEWPORT), DRAWER_WIDTH_MIN);
});

test('toggle: a width very close to but still under the max still expands', () => {
  // Exactly one pixel under the max is treated as "not yet expanded", so the
  // next click is still an expand, not a collapse.
  assert.equal(nextToggleDrawerWidth(NARROW_MAX - 1, NARROW_VIEWPORT), NARROW_MAX);
});

test('toggle: stored value survives a remount (localStorage round-trip)', () => {
  // Simulate the first mount: viewport is wide enough that the toggle
  // expands the drawer to 90% and the toggle writes that width to
  // localStorage.
  const storage = new InMemoryStorage();
  assert.equal(storage.getItem(DRAWER_WIDTH_STORAGE_KEY), null);

  // First user expands the drawer.
  const firstMountWidth = DRAWER_WIDTH_DEFAULT;
  const expanded = nextToggleDrawerWidth(firstMountWidth, NARROW_VIEWPORT);
  assert.equal(expanded, NARROW_MAX);
  assert.equal(writeDrawerWidth(storage, expanded), true);
  assert.equal(storage.getItem(DRAWER_WIDTH_STORAGE_KEY), String(NARROW_MAX));

  // The component unmounts and remounts (route change, refresh, etc).
  // readDrawerWidth must hand the same width back from storage so the user
  // does not see the drawer snap back to 410.
  const secondMountWidth = readDrawerWidth(storage, NARROW_VIEWPORT);
  assert.equal(secondMountWidth, NARROW_MAX);

  // The remounted toggle now collapses it back to the min.
  const collapsed = nextToggleDrawerWidth(secondMountWidth, NARROW_VIEWPORT);
  assert.equal(collapsed, DRAWER_WIDTH_MIN);
  assert.equal(writeDrawerWidth(storage, collapsed), true);
  assert.equal(storage.getItem(DRAWER_WIDTH_STORAGE_KEY), String(DRAWER_WIDTH_MIN));
});

test('toggle: a persisted value out of range for the new viewport is clamped on read', () => {
  // Persist a wide-viewport max on storage, then read back on a narrower
  // viewport. The component must clamp to the new max instead of honouring
  // the stale value.
  const storage = new InMemoryStorage();
  writeDrawerWidth(storage, WIDE_MAX);
  assert.equal(readDrawerWidth(storage, NARROW_VIEWPORT), NARROW_MAX);
});

test('read: storage that throws on access returns the default 410', () => {
  const throwingStorage = new ThrowingStorage();
  assert.equal(readDrawerWidth(throwingStorage, NARROW_VIEWPORT), DRAWER_WIDTH_DEFAULT);
});

test('read: storage that throws returns the default even when a value would have been valid', () => {
  // Defensive coverage: throwing storage must always fall back, never
  // partially apply the previously-cached value or return NaN.
  const throwingStorage = new ThrowingStorage();
  const result = readDrawerWidth(throwingStorage, WIDE_VIEWPORT);
  assert.equal(Number.isFinite(result), true);
  assert.equal(result, DRAWER_WIDTH_DEFAULT);
});

test('read: storage missing returns the default 410', () => {
  // No storage means the same default must apply.
  assert.equal(readDrawerWidth(null, NARROW_VIEWPORT), DRAWER_WIDTH_DEFAULT);
  assert.equal(readDrawerWidth(undefined, NARROW_VIEWPORT), DRAWER_WIDTH_DEFAULT);
});

test('read: a malformed (non-numeric) persisted value falls back to the default 410', () => {
  const storage = new InMemoryStorage();
  storage.setItem(DRAWER_WIDTH_STORAGE_KEY, 'not a number');
  assert.equal(readDrawerWidth(storage, NARROW_VIEWPORT), DRAWER_WIDTH_DEFAULT);
});

test('write: returns false when storage throws, but does not propagate the throw', () => {
  const throwingStorage = new ThrowingStorage();
  // writeDrawerWidth is the in-memory state's safety net: the toggle still
  // updates state even when persistence fails, so a throw must not escape.
  assert.equal(writeDrawerWidth(throwingStorage, 900), false);
});

test('write: returns false when storage is unavailable', () => {
  assert.equal(writeDrawerWidth(null, 900), false);
  assert.equal(writeDrawerWidth(undefined, 900), false);
});

test('storage key and default width match the CSS fallback (--drawer-width, 410px)', () => {
  // style.css uses `width: min(var(--drawer-width, 410px), 92vw);` so the
  // fallback value has to be the minimum width we also enforce here. If
  // anyone ever changes DRAWER_WIDTH_DEFAULT they must update the CSS
  // fallback in lockstep.
  assert.equal(DRAWER_WIDTH_DEFAULT, DRAWER_WIDTH_MIN);
  assert.equal(DRAWER_WIDTH_DEFAULT, 410);
  assert.equal(DRAWER_WIDTH_STORAGE_KEY, 'lhh.detailsWidth');
});

// In-memory state store used to simulate window.localStorage without touching
// the real one. Kept local to this file so a test failure points here.
class InMemoryStorage implements Pick<Storage, 'getItem' | 'setItem'> {
  private readonly values = new Map<string, string>();
  getItem(key: string): string | null {
    return this.values.has(key) ? this.values.get(key)! : null;
  }
  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

// Storage that always throws on access — matches Safari private mode and
// quota-exceeded scenarios where localStorage is present but unusable.
class ThrowingStorage implements Pick<Storage, 'getItem' | 'setItem'> {
  getItem(_key: string): string | null {
    throw new Error('localStorage disabled');
  }
  setItem(_key: string, _value: string): void {
    throw new Error('localStorage disabled');
  }
}