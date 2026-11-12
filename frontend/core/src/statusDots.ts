/**
 * The single source of truth for run/queue status → dot appearance.
 *
 * Every surface that renders a status dot (the sidebar task list, the Run
 * status panel, and the queue view) must import from this module so that a
 * status always looks the same wherever it appears.  Two rules make the dots
 * readable at a glance:
 *
 *   1. Colour is never the only cue.  Each status group also has a distinct
 *      *shape* (`solid`, `ring`, `outline`, `cross`, `slash` or `bar`), so a
 *      user who cannot distinguish the hues still sees the difference.
 *   2. The dot always carries the status word in a `title` tooltip and an
 *      `aria-label`, so assistive tech and hover both announce the state.
 *
 * The colours are the fleet.easybutt0n.ai "Ship Plane" status tokens.  Every
 * foreground in {@link STATUS_DOT_CONTRAST} clears WCAG AA 3:1 against both
 * sidebar backgrounds (`#fcfcfc` light, `#171717` dark).
 */

/** The distinct visual families a status dot can belong to. */
export type StatusDotShape =
  /** Filled circle with a soft halo. */
  | 'solid'
  /** Filled circle inside a thick ring. */
  | 'ring'
  /** Hollow circle with a coloured border. */
  | 'outline'
  /** Filled circle with a diagonal slash. */
  | 'slash'
  /** Filled circle with a horizontal bar across it. */
  | 'bar'
  /** Filled circle with a cross. */
  | 'cross';

export type StatusDotTone =
  /** Actively working. Green, slow pulse. */
  | 'running'
  /** Gated on a human. Amber. */
  | 'waiting'
  /** Not started yet. Grey, hollow. */
  | 'pending'
  /** Ended badly. Red. */
  | 'failed'
  /** Ended well. Blue/teal. */
  | 'done'
  /** Ended without completing. Grey, solid. */
  | 'cancelled';

export interface StatusDotStyle {
  /** The colour group this status renders with. */
  readonly tone: StatusDotTone;
  /** The non-colour cue, so the dot is distinguishable without colour. */
  readonly shape: StatusDotShape;
  /** The CSS custom property holding this tone's foreground colour. */
  readonly colorVar: string;
  /** The CSS custom property holding this tone's background/ring colour. */
  readonly backgroundVar: string;
  /** True when the dot should animate (running only). */
  readonly pulse: boolean;
  /** The CSS class that carries the tone + shape to the stylesheet. */
  readonly className: string;
}

/** Every status word the backend can emit for a run or queue entry.
 *
 * The list is the union of the supervised-run lifecycle
 * (`src/lh_harness/supervisor/lifecycle.py`), the status words the queue
 * store writes (`src/lh_harness/queue.py`), the live-state words the
 * retention sweep reads (`src/lh_harness/disk_retention/runs.py`), the
 * park/resume states the workspace manager writes, and the approval words
 * the web API returns.  The unit test in
 * `frontend/core/test/statusDots.test.ts` enumerates the backend source and
 * asserts that this set stays in sync with the backend vocabulary, so a new
 * backend status cannot land without a dot style.
 */
export const BACKEND_STATUS_VALUES: readonly string[] = Object.freeze([
  // Supervised-run lifecycle (supervisor/lifecycle.py).
  'idle',
  'creating',
  'starting',
  'running',
  'waiting_approval',
  'stopping',
  'aborting',
  'completed',
  'complete',
  'done',
  'success',
  'succeeded',
  'finished',
  'ok',
  'passed',
  'failed',
  'failure',
  'error',
  'errored',
  'timeout',
  'timed_out',
  'blocked',
  'incomplete',
  'needs_revision',
  'rejected',
  'cancelled',
  'canceled',
  'stopped',
  'aborted',
  'skipped',
  // Waiting / gating vocabulary.
  'waiting',
  'waiting-approval',
  'approval',
  'paused',
  'reconnecting',
  'gated',
  // Not-started vocabulary.
  'pending',
  'queued',
  'suspended',
  'active',
  'unknown',
  // Queue vocabulary (queue.py `_VALID_STATUS`): a queued entry that has
  // been handed to the launcher but whose run has not appeared yet.
  'launched',
  'launching',
  // Resume / park vocabulary.
  'resuming',
  'parked',
  // Control-bus and approval receipt vocabulary.
  'applied',
  'accepted',
  'resolved',
  'deleted',
]);

/** Status word → tone. */
const TONE_BY_STATUS: ReadonlyMap<string, StatusDotTone> = new Map([
  // Running family: the run is live and making progress.
  ['running', 'running'],
  ['starting', 'running'],
  ['creating', 'running'],
  ['active', 'running'],
  // A queue entry that has been handed to the launcher.  It is in flight,
  // not waiting, so it reads as live work rather than as "not started yet".
  ['launched', 'running'],
  ['launching', 'running'],
  ['resuming', 'running'],
  // Teardown: the run is on its way out but has not reached a terminal state.
  ['stopping', 'cancelled'],
  ['aborting', 'cancelled'],
  // Waiting family (gated on a human or on a reconnect).
  ['waiting_approval', 'waiting'],
  ['waiting-approval', 'waiting'],
  ['waiting', 'waiting'],
  ['approval', 'waiting'],
  ['paused', 'waiting'],
  ['reconnecting', 'waiting'],
  ['gated', 'waiting'],
  // Pending family (accepted but not started).
  ['pending', 'pending'],
  ['queued', 'pending'],
  ['suspended', 'pending'],
  ['idle', 'pending'],
  ['parked', 'pending'],
  ['unknown', 'pending'],
  // Failure family.
  ['failed', 'failed'],
  ['failure', 'failed'],
  ['error', 'failed'],
  ['errored', 'failed'],
  ['timeout', 'failed'],
  ['timed_out', 'failed'],
  ['blocked', 'failed'],
  ['incomplete', 'failed'],
  ['needs_revision', 'failed'],
  ['rejected', 'failed'],
  // Done family.
  ['completed', 'done'],
  ['complete', 'done'],
  ['done', 'done'],
  ['success', 'done'],
  ['succeeded', 'done'],
  ['passed', 'done'],
  ['ok', 'done'],
  ['finished', 'done'],
  ['applied', 'done'],
  ['accepted', 'done'],
  ['resolved', 'done'],
  // Cancelled family.
  ['cancelled', 'cancelled'],
  ['canceled', 'cancelled'],
  ['stopped', 'cancelled'],
  ['aborted', 'cancelled'],
  ['skipped', 'cancelled'],
  ['deleted', 'cancelled'],
]);

/**
 * The non-colour cue per tone.
 *
 * Every tone has a different shape so the dots stay distinguishable for
 * users who cannot rely on hue, and so `pending` (hollow outline) is visibly
 * different from `cancelled` (solid grey).
 */
const SHAPE_BY_TONE: Readonly<Record<StatusDotTone, StatusDotShape>> = Object.freeze({
  running: 'ring',
  waiting: 'bar',
  pending: 'outline',
  failed: 'cross',
  done: 'solid',
  cancelled: 'slash',
});

const COLOR_VAR_BY_TONE: Readonly<Record<StatusDotTone, string>> = Object.freeze({
  running: '--status-running',
  waiting: '--status-waiting',
  pending: '--status-pending',
  failed: '--status-failed',
  done: '--status-done',
  cancelled: '--status-cancelled',
});

const BACKGROUND_VAR_BY_TONE: Readonly<Record<StatusDotTone, string>> = Object.freeze({
  running: '--status-running-soft',
  waiting: '--status-waiting-soft',
  pending: '--status-pending-soft',
  failed: '--status-failed-soft',
  done: '--status-done-soft',
  cancelled: '--status-cancelled-soft',
});

/** The tone used for any status the backend has not taught us about. */
export const UNKNOWN_STATUS_TONE: StatusDotTone = 'pending';

/** Normalise a raw status word the way the rest of the UI does. */
export function normalizeStatus(status: unknown): string {
  return String(status ?? '').trim().toLowerCase().replace(/\s+/g, '_');
}

/**
 * Look up the dot style for a status.
 *
 * Unknown statuses fall back to the neutral pending tone (grey) rather than
 * guessing a semantic; the caller still shows the raw status word so the
 * fallback is never silent.
 */
export function statusDotStyle(status: unknown): StatusDotStyle {
  const key = normalizeStatus(status);
  const tone = TONE_BY_STATUS.get(key) ?? UNKNOWN_STATUS_TONE;
  const shape = SHAPE_BY_TONE[tone];
  return {
    tone,
    shape,
    colorVar: COLOR_VAR_BY_TONE[tone],
    backgroundVar: BACKGROUND_VAR_BY_TONE[tone],
    pulse: tone === 'running',
    className: `status-dot status-${tone} status-shape-${shape}`,
  };
}

/**
 * The class list for a status dot, including the legacy per-status class
 * names so existing stylesheet rules keep working.
 */
export function statusDotClassName(status: unknown): string {
  const key = normalizeStatus(status);
  const style = statusDotStyle(status);
  const legacy = key ? `status-${key.replace(/_/g, '-')}` : '';
  return legacy ? `${style.className} ${legacy}` : style.className;
}

/**
 * The accessible label for a status dot.  It always contains the status
 * word itself so the state is announced even when the dot is tiny.
 */
export function statusDotLabel(status: unknown, fallbackLabel?: string): string {
  const key = normalizeStatus(status);
  if (!key) return fallbackLabel || 'Unknown';
  const pretty = key.replace(/_/g, ' ');
  return fallbackLabel ? `${pretty} · ${fallbackLabel}` : pretty;
}

/**
 * The attributes every status dot should spread onto its element.
 *
 * `title` gives the hover tooltip and `aria-label` gives the same word to
 * assistive technology, so colour is never the only signal.
 */
export function statusDotAttributes(
  status: unknown,
  options: { label?: string; role?: string } = {},
): { className: string; title: string; 'aria-label': string; role?: string; 'data-status-tone'?: string } {
  const style = statusDotStyle(status);
  const label = statusDotLabel(status, options.label);
  const attributes: {
    className: string;
    title: string;
    'aria-label': string;
    role?: string;
    'data-status-tone'?: string;
  } = {
    className: statusDotClassName(status),
    title: label,
    'aria-label': label,
    'data-status-tone': style.tone,
  };
  if (options.role) attributes.role = options.role;
  return attributes;
}

/**
 * The WCAG contrast evidence for the palette, computed against the two
 * sidebar backgrounds the workbench uses.
 *
 * `ratio` is the WCAG 2.x relative-luminance contrast ratio; every value
 * must be >= 3 (WCAG AA for non-text/UI components) in both themes.
 *
 * The hex foregrounds and the ratios are **derived from the shipped CSS**
 * (`frontend/web/src/style.css`) *as the browser resolves it*, not written
 * by hand and not read off a raw token: the unit test in
 * `frontend/core/test/statusDots.test.ts` parses that stylesheet, replays
 * the cascade (selector specificity and source order) for the dot classes
 * the mapping actually emits, resolves the `var(--status-*)` custom
 * properties for each theme, and recomputes every ratio from the painted
 * foreground and the painted sidebar background.  A colour change in the
 * sheet — or a later rule that overrides a dot's colour — fails the test
 * rather than leaving a stale figure behind.
 * `lightCssValue` / `darkCssValue` record the literal CSS declaration each
 * hex was converted from, so the table is traceable to the stylesheet.
 */
export const STATUS_DOT_CONTRAST: readonly {
  tone: StatusDotTone;
  lightForeground: string;
  lightCssValue: string;
  lightBackground: string;
  lightRatio: number;
  darkForeground: string;
  darkCssValue: string;
  darkBackground: string;
  darkRatio: number;
}[] = Object.freeze([
  { tone: 'running', lightForeground: '#12873d', lightCssValue: 'hsl(142 76% 30%)', lightBackground: '#fcfcfc', lightRatio: 4.49, darkForeground: '#2edc6d', darkCssValue: 'hsl(142 71% 52%)', darkBackground: '#171717', darkRatio: 9.88 },
  { tone: 'waiting', lightForeground: '#9f5704', lightCssValue: 'hsl(32 95% 32%)', lightBackground: '#fcfcfc', lightRatio: 5.33, darkForeground: '#f7c222', darkCssValue: 'hsl(45 93% 55%)', darkBackground: '#171717', darkRatio: 10.84 },
  { tone: 'pending', lightForeground: '#55616d', lightCssValue: 'hsl(210 12% 38%)', lightBackground: '#fcfcfc', lightRatio: 6.17, darkForeground: '#a0a8b1', darkCssValue: 'hsl(210 10% 66%)', darkBackground: '#171717', darkRatio: 7.45 },
  { tone: 'failed', lightForeground: '#b81e1e', lightCssValue: 'hsl(0 72% 42%)', lightBackground: '#fcfcfc', lightRatio: 6.31, darkForeground: '#df4949', darkCssValue: 'hsl(0 70% 58%)', darkBackground: '#171717', darkRatio: 4.43 },
  { tone: 'done', lightForeground: '#0a6f9e', lightCssValue: 'hsl(199 88% 33%)', lightBackground: '#fcfcfc', lightRatio: 5.41, darkForeground: '#19acf0', darkCssValue: 'hsl(199 88% 52%)', darkBackground: '#171717', darkRatio: 7.0 },
  { tone: 'cancelled', lightForeground: '#6a737c', lightCssValue: 'hsl(210 8% 45%)', lightBackground: '#fcfcfc', lightRatio: 4.7, darkForeground: '#9199a1', darkCssValue: 'hsl(210 8% 60%)', darkBackground: '#171717', darkRatio: 6.21 },
]);
