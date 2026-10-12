// LHH-DETAILS-PANEL-a: width clamp, default, and toggle logic for the right-hand
// Details slide-over. Extracted out of DetailsDrawer so the same behavior can
// be exercised from node:test without standing up a DOM. The clamp and the
// toggle are pure functions of (value, viewport); the storage read/write is a
// small adapter that DetailsDrawer feeds with window.localStorage at mount
// time so test code can stub it without touching global state directly.

export const DRAWER_WIDTH_STORAGE_KEY = 'lhh.detailsWidth';
export const DRAWER_WIDTH_MIN = 410;

/** Largest permitted width in CSS pixels (90% of the current viewport). */
export function drawerWidthMaxFor(viewportWidth: number): number {
  const safeViewport = Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 0;
  return Math.max(DRAWER_WIDTH_MIN, Math.floor(0.9 * safeViewport));
}

/**
 * Clamp any candidate width to the live drawer's allowed range:
 *   min = DRAWER_WIDTH_MIN (410 px), max = floor(0.9 * viewport).
 * Anything below the min or above the max snaps to the boundary; non-finite
 * inputs fall back to the min.
 */
export function clampDrawerWidth(value: number, viewportWidth: number): number {
  const min = DRAWER_WIDTH_MIN;
  const max = drawerWidthMaxFor(viewportWidth);
  if (!Number.isFinite(value)) return min;
  const rounded = Math.round(value);
  if (rounded < min) return min;
  if (rounded > max) return max;
  return rounded;
}

/** Default width when nothing is persisted or storage is unavailable. */
export const DRAWER_WIDTH_DEFAULT = DRAWER_WIDTH_MIN;

/**
 * Read the persisted width. Returns `DRAWER_WIDTH_DEFAULT` when storage is
 * unavailable, the stored value is missing, or the stored value cannot be
 * parsed as an integer. Clamps to the live viewport before returning.
 */
export function readDrawerWidth(
  storage: Pick<Storage, 'getItem'> | null | undefined,
  viewportWidth: number,
): number {
  if (!storage) return clampDrawerWidth(DRAWER_WIDTH_DEFAULT, viewportWidth);
  try {
    const stored = storage.getItem(DRAWER_WIDTH_STORAGE_KEY);
    if (stored === null || stored === undefined) {
      return clampDrawerWidth(DRAWER_WIDTH_DEFAULT, viewportWidth);
    }
    const parsed = Number.parseInt(stored, 10);
    if (!Number.isFinite(parsed)) {
      return clampDrawerWidth(DRAWER_WIDTH_DEFAULT, viewportWidth);
    }
    return clampDrawerWidth(parsed, viewportWidth);
  } catch {
    // Storage may be disabled (private mode, quota); fall through to default.
    return clampDrawerWidth(DRAWER_WIDTH_DEFAULT, viewportWidth);
  }
}

/**
 * Decide which width the toggle button should move to next. When the drawer
 * is already at-or-above the live max it collapses to the min (410 px);
 * otherwise it expands to the live max. Pure: no React, no storage.
 */
export function nextToggleDrawerWidth(currentWidth: number, viewportWidth: number): number {
  const min = DRAWER_WIDTH_MIN;
  const max = drawerWidthMaxFor(viewportWidth);
  const next = currentWidth >= max ? min : max;
  return clampDrawerWidth(next, viewportWidth);
}

/**
 * Persist the width to storage. Returns true on success, false when storage
 * is unavailable or throws. The in-memory state update happens separately;
 * callers must not treat a `false` return as a failure to update width.
 */
export function writeDrawerWidth(
  storage: Pick<Storage, 'setItem'> | null | undefined,
  width: number,
): boolean {
  if (!storage) return false;
  try {
    storage.setItem(DRAWER_WIDTH_STORAGE_KEY, String(width));
    return true;
  } catch {
    return false;
  }
}