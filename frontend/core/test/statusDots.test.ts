import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { test } from 'node:test';
import {
  BACKEND_STATUS_VALUES,
  STATUS_DOT_CONTRAST,
  UNKNOWN_STATUS_TONE,
  normalizeStatus,
  statusDotAttributes,
  statusDotClassName,
  statusDotLabel,
  statusDotStyle,
  type StatusDotShape,
  type StatusDotTone,
} from '../src/statusDots';

// The suite is bundled into a temp directory before it runs, so the repo root
// has to come from the working directory (frontend/core) rather than from
// import.meta.url.
const repoRoot = resolve(process.cwd(), '../..');

function readBackend(relative: string): string {
  return readFileSync(resolve(repoRoot, relative), 'utf8');
}

/**
 * The backend modules that define or write a run/queue status, plus the
 * specific constructs in them that carry an authoritative status vocabulary.
 */
const STATUS_SOURCES = [
  'src/lh_harness/supervisor/lifecycle.py',
  'src/lh_harness/supervisor/service.py',
  'src/lh_harness/supervisor/control_bus.py',
  'src/lh_harness/webapi/snapshot.py',
  'src/lh_harness/webapi/server.py',
  'src/lh_harness/queue.py',
  'src/lh_harness/pg_queue.py',
  'src/lh_harness/queue_reconcile.py',
  'src/lh_harness/launcher.py',
  'src/lh_harness/dashboard/state.py',
  'src/lh_harness/disk_retention/runs.py',
  'src/lh_harness/mcp_tools.py',
  'src/lh_harness/manager.py',
  'src/lh_harness/types.py',
  'src/lh_harness/workspace_park.py',
] as const;

/**
 * The frontend's own declared status vocabulary.
 *
 * Some status words the UI recognises never appear in the Python backend:
 * `aborting` and the `stop_requested` / `abort_requested` request words are
 * declared by the web client (`frontend/core/src/commands.ts` and
 * `frontend/web/src/App.tsx`) because the API may surface them.  A status
 * word the UI renders still needs a dot, so the discovery covers these
 * declarations too — otherwise `aborting` would be styled by assertion
 * rather than by proof.
 */
const FRONTEND_STATUS_SOURCES = [
  'frontend/core/src/commands.ts',
  'frontend/web/src/App.tsx',
] as const;

/**
 * The constructs that carry an *authoritative* status vocabulary: a named
 * frozenset of statuses, or a named tuple of statuses.  These are the
 * places where the backend itself declares which status words exist, so
 * they are enumerated in full (every quoted word in the literal) rather
 * than filtered by assignment shape.
 */
const VOCABULARY_DECLARATIONS: readonly { file: string; pattern: RegExp }[] = [
  { file: 'src/lh_harness/supervisor/lifecycle.py', pattern: /STATUS_ALIASES:\s*dict\[str,\s*str\]\s*=\s*\{([\s\S]*?)\n\}/ },
  { file: 'src/lh_harness/supervisor/lifecycle.py', pattern: /TERMINAL_STATUSES\s*=\s*frozenset\(\{([\s\S]*?)\}\)/ },
  { file: 'src/lh_harness/supervisor/lifecycle.py', pattern: /ACTIVE_STATUSES\s*=\s*frozenset\(\{([\s\S]*?)\}\)/ },
  { file: 'src/lh_harness/queue.py', pattern: /_VALID_STATUS\s*=\s*frozenset\(\{([\s\S]*?)\}\)/ },
  { file: 'src/lh_harness/queue.py', pattern: /_NON_TERMINAL_STATUS\s*=\s*frozenset\(\{([\s\S]*?)\}\)/ },
  { file: 'src/lh_harness/queue_reconcile.py', pattern: /_RECONCILE_ENTRY_STATES\s*=\s*frozenset\(\{([\s\S]*?)\}\)/ },
  { file: 'src/lh_harness/supervisor/control_bus.py', pattern: /terminal_receipt_statuses\s*=\s*frozenset\(\{([\s\S]*?)\}\)/ },
  { file: 'src/lh_harness/disk_retention/runs.py', pattern: /LIVE_STATES:\s*frozenset\[str\]\s*=\s*frozenset\(\{([\s\S]*?)\}\)/ },
  { file: 'src/lh_harness/webapi/server.py', pattern: /_SUSPEND_OPEN_STATES\s*=\s*\(([\s\S]*?)\)/ },
  { file: 'src/lh_harness/types.py', pattern: /status:\s*Literal\[([\s\S]*?)\]/ },
];

/**
 * The assignment shapes the backend uses to write a status word.  Each
 * pattern captures the status word(s) being written.
 *
 * The attribute-assignment form matters: the queue writes
 * `entry.status = "launched"` and the dashboard writes
 * `updated["status"] = "stopping"`.  A discovery mechanism that only looks
 * for `"status": "..."` misses both.
 */
const STATUS_ASSIGNMENTS: readonly RegExp[] = [
  // entry.status = "launched"  /  record.status = "stopping"
  /\.status\s*=\s*"([a-z_]+)"/g,
  // updated["status"] = "stopping"  /  merged['status'] = 'x'
  /\[\s*["']status["']\s*\]\s*=\s*["']([a-z_]+)["']/g,
  // "status": "launched"  (dict literal)
  /["']status["']\s*:\s*["']([a-z_]+)["']/g,
  // status="launched"  (keyword argument)
  /(?<![.\w])status\s*=\s*"([a-z_]+)"/g,
  // mark_run(manifest_record, "paused", None)
  /mark_run\(\s*[^,]+,\s*"([a-z_]+)"/g,
  // canonical_lifecycle_status("running")
  /canonical_lifecycle_status\(\s*"([a-z_]+)"/g,
  // status IN ('pending', 'launched', 'blocked')
  /status\s+(?:NOT\s+)?IN\s*\(([^)]*)\)/gi,
];

/** Words that are not statuses even though they appear in the same modules. */
const NON_STATUS_WORDS = new Set([
  'status', 'statuses', 'state', 'states', 'name', 'id', 'control',
  'resume_epoch', '_', 'pid', 'pgid', 'command', 'exit_code',
  'failure_reason', 'requested_action', 'stop_requested_at',
  'abort_requested_at', 'attached', 'cgroup', 'signal_mode', 'workspace',
  'model', 'agent', 'role', 'task', 'summary', 'payload', 'run_id',
  'mcp_profile', 'reasoning_effort', 'created_at', 'updated_at',
  'started_at', 'finished_at', 'observed_at', 'recorded_at', 'at',
]);

/** Pull every lowercase status word the backend can write for a run. */
function backendRunStatusWords(): Set<string> {
  const words = new Set<string>();

  // 1. The authoritative vocabularies: every quoted word in a declared
  //    status set or Literal is a status word by definition.
  for (const { file, pattern } of VOCABULARY_DECLARATIONS) {
    const text = readBackend(file);
    const match = text.match(pattern);
    if (!match) continue;
    for (const word of match[1].matchAll(/["']([a-z_]+)["']/g)) {
      words.add(word[1]);
    }
  }

  // 2. Every assignment that writes a status word, in any of the shapes the
  //    backend uses.
  for (const file of STATUS_SOURCES) {
    const text = readBackend(file);
    for (const pattern of STATUS_ASSIGNMENTS) {
      for (const match of text.matchAll(pattern)) {
        const captured = match[1];
        if (captured.includes(',')) {
          // A tuple form such as ('pending', 'launched', 'blocked').
          for (const word of captured.matchAll(/["']([a-z_]+)["']/g)) {
            words.add(word[1]);
          }
        } else {
          words.add(captured);
        }
      }
    }
  }

  for (const word of [...words]) {
    if (NON_STATUS_WORDS.has(word)) words.delete(word);
  }
  return words;
}

/**
 * The status words the web client declares.
 *
 * The frontend names the statuses it renders in `new Set([...])` literals:
 * the stopping set (`stopping`, `aborting`, `stop_requested`,
 * `abort_requested`), the terminal set, and the per-language status label
 * maps.  Every quoted word in those declarations is a status word the UI
 * can put on screen, so it needs a dot as well.
 */
function frontendStatusWords(): Set<string> {
  const words = new Set<string>();
  for (const file of FRONTEND_STATUS_SOURCES) {
    const text = readBackend(file);
    // `new Set([...])` of quoted words.
    for (const match of text.matchAll(/new Set\(\[([^\]]*)\]\)/g)) {
      for (const word of match[1].matchAll(/["']([a-z_]+)["']/g)) {
        words.add(word[1]);
      }
    }
    // A status-label map: `running: 'Running', waiting_approval: '...'`.
    for (const match of text.matchAll(/([a-z_]+)\s*:\s*['"`][^'"`]*['"`]/g)) {
      words.add(match[1]);
    }
  }
  for (const word of [...words]) {
    if (NON_STATUS_WORDS.has(word)) words.delete(word);
  }
  return words;
}

/** Every status word the system can put on screen, from either side. */
function everyStatusWord(): Set<string> {
  const words = new Set<string>(backendRunStatusWords());
  for (const word of frontendStatusWords()) words.add(word);
  return words;
}

test('the status discovery mechanism sees every backend status word', () => {
  const words = backendRunStatusWords();
  // The regression this guards: a discovery mechanism that only matched
  // `"status": "..."` missed `entry.status = "launched"`, the queue's real
  // non-terminal status, so the queue view showed a grey dot for a run that
  // was in fact in flight.
  assert.ok(words.has('launched'), 'discovery missed the queue status "launched"');
  assert.ok(words.has('pending'), 'discovery missed "pending"');
  assert.ok(words.has('running'), 'discovery missed "running"');
  assert.ok(words.has('waiting_approval'), 'discovery missed "waiting_approval"');
  assert.ok(words.has('stopping'), 'discovery missed "stopping"');
  assert.ok(words.has('blocked'), 'discovery missed "blocked"');
  assert.ok(words.has('done'), 'discovery missed "done"');
  assert.ok(words.has('failed'), 'discovery missed "failed"');
  assert.ok(words.has('cancelled'), 'discovery missed "cancelled"');
  assert.ok(words.has('gated'), 'discovery missed the retention live state "gated"');
  assert.ok(words.has('resuming'), 'discovery missed the retention live state "resuming"');
});

test('every backend run status has a dot style', () => {
  const backendWords = backendRunStatusWords();
  const scope = [...backendWords].filter((word) => !NON_STATUS_WORDS.has(word));
  assert.ok(scope.length > 10, `expected to discover backend statuses, got ${scope.length}`);
  const unmapped = scope.filter((word) => {
    const style = statusDotStyle(word);
    return !style.colorVar || !style.className;
  });
  assert.deepEqual(unmapped, [], `backend statuses without a dot style: ${unmapped.join(', ')}`);
});

test('every status the UI can render has a dot style', () => {
  // The web client declares status words the Python backend never emits
  // (`aborting`, `stop_requested`, `abort_requested`).  They still reach a
  // dot, so the discovery covers the frontend's own declarations and the
  // mapping has to cover what it finds.
  const words = everyStatusWord();
  const scope = [...words].filter((word) => !NON_STATUS_WORDS.has(word));
  assert.ok(scope.length > 10, `expected to discover statuses, got ${scope.length}`);
  const unmapped = scope.filter((word) => {
    const style = statusDotStyle(word);
    return !style.colorVar || !style.className || !style.shape;
  });
  assert.deepEqual(unmapped, [], `statuses without a dot style: ${unmapped.join(', ')}`);
});

test('the discovery sees the teardown statuses the UI renders', () => {
  const words = everyStatusWord();
  // `stopping` is written by the backend (supervisor/service.py).
  assert.ok(words.has('stopping'), 'discovery missed "stopping"');
  // `aborting` is declared by the web client (core/src/commands.ts and
  // web/src/App.tsx); the backend reports both teardown paths as
  // "stopping", but the UI must still be able to style "aborting".
  assert.ok(words.has('aborting'), 'discovery missed "aborting"');
  // The queue's real in-flight status, written as `entry.status = "launched"`.
  assert.ok(words.has('launched'), 'discovery missed the queue status "launched"');
});

test('every declared backend status value has a dot style', () => {
  for (const status of BACKEND_STATUS_VALUES) {
    const style = statusDotStyle(status);
    assert.ok(style.className, `no class for ${status}`);
    assert.ok(style.colorVar.startsWith('--status-'), `no colour token for ${status}`);
    assert.ok(style.backgroundVar.startsWith('--status-'), `no background token for ${status}`);
  }
});

test('the declared backend status list covers the canonical lifecycle words', () => {
  const declared = new Set(BACKEND_STATUS_VALUES);
  for (const word of [
    'idle', 'creating', 'starting', 'running', 'waiting_approval', 'stopping',
    'aborting', 'completed', 'complete', 'done', 'success', 'succeeded',
    'finished', 'ok', 'passed', 'failed', 'failure', 'error', 'errored',
    'timeout', 'timed_out', 'blocked', 'incomplete', 'needs_revision',
    'rejected', 'cancelled', 'canceled', 'stopped', 'aborted', 'skipped',
    'waiting', 'approval', 'paused', 'reconnecting', 'pending', 'queued',
    'suspended', 'active', 'unknown',
    // The queue's own non-terminal status (queue.py `_VALID_STATUS`).
    'launched',
    // The retention sweep's live states (disk_retention/runs.py).
    'gated', 'resuming',
  ]) {
    assert.ok(declared.has(word), `declared status list is missing ${word}`);
  }
});

test('unknown statuses fall back to grey pending plus the status word', () => {
  const style = statusDotStyle('totally-unknown-status');
  assert.equal(style.tone, UNKNOWN_STATUS_TONE);
  assert.equal(style.tone, 'pending');
  assert.equal(style.shape, 'outline');
  assert.equal(style.colorVar, '--status-pending');
  assert.equal(style.pulse, false);

  const label = statusDotLabel('totally-unknown-status');
  assert.match(label, /totally-unknown-status/);
  assert.ok(label.length > 0);
});

test('an empty or missing status also falls back to grey', () => {
  for (const value of ['', '   ', null, undefined]) {
    assert.equal(statusDotStyle(value).tone, 'pending');
    assert.equal(statusDotStyle(value).colorVar, '--status-pending');
  }
});

test('the required status families map to the expected tones', () => {
  const expected: [string, StatusDotTone][] = [
    ['running', 'running'],
    ['starting', 'running'],
    ['creating', 'running'],
    ['waiting_approval', 'waiting'],
    ['waiting', 'waiting'],
    ['paused', 'waiting'],
    ['pending', 'pending'],
    ['queued', 'pending'],
    ['failed', 'failed'],
    ['error', 'failed'],
    ['blocked', 'failed'],
    ['done', 'done'],
    ['completed', 'done'],
    ['success', 'done'],
    ['cancelled', 'cancelled'],
    ['stopped', 'cancelled'],
    ['aborted', 'cancelled'],
    // The queue's own in-flight status reads as live work, not as
    // "not started yet".
    ['launched', 'running'],
    ['launching', 'running'],
    ['resuming', 'running'],
    // Teardown states are neither running nor terminal yet.
    ['stopping', 'cancelled'],
    ['aborting', 'cancelled'],
    ['gated', 'waiting'],
    ['parked', 'pending'],
  ];
  for (const [status, tone] of expected) {
    assert.equal(statusDotStyle(status).tone, tone, `${status} should be ${tone}`);
  }
});

test('colour is never the only cue: every tone has its own shape', () => {
  const shapes = new Map<StatusDotTone, StatusDotShape>();
  for (const status of BACKEND_STATUS_VALUES) {
    const { tone, shape } = statusDotStyle(status);
    assert.ok(shape, `${status} has no shape`);
    const existing = shapes.get(tone);
    if (existing) {
      assert.equal(existing, shape, `tone ${tone} must map to exactly one shape`);
    } else {
      shapes.set(tone, shape);
    }
  }
  // The six families must be mutually distinguishable without colour.
  assert.equal(shapes.size, 6, `expected 6 distinct shapes, got ${shapes.size}`);
  assert.equal(new Set(shapes.values()).size, 6);
});

test('running pulses and no other tone does', () => {
  for (const status of BACKEND_STATUS_VALUES) {
    const style = statusDotStyle(status);
    const shouldPulse = style.tone === 'running';
    assert.equal(style.pulse, shouldPulse, `${status} pulse mismatch`);
  }
});

test('the dot attributes carry the status word in a tooltip and aria-label', () => {
  const attributes = statusDotAttributes('waiting_approval');
  assert.match(attributes.title, /waiting approval/);
  assert.match(attributes['aria-label'], /waiting approval/);
  assert.ok(attributes.className.includes('status-dot'));
  assert.ok(attributes.className.includes('status-waiting'));

  const withFallback = statusDotAttributes('running', { label: 'Input required' });
  assert.match(withFallback.title, /running/i);
  assert.match(withFallback.title, /Input required/);
  assert.match(withFallback['aria-label'], /running/i);
});

test('the class name carries the tone, the shape and the legacy per-status class', () => {
  assert.equal(statusDotClassName('running'), 'status-dot status-running status-shape-ring status-running');
  assert.equal(statusDotClassName('waiting_approval'), 'status-dot status-waiting status-shape-bar status-waiting-approval');
  assert.equal(statusDotClassName('unknown-thing'), 'status-dot status-pending status-shape-outline status-unknown-thing');
  assert.ok(statusDotClassName('').startsWith('status-dot status-pending'));
});

test('status normalisation matches the rest of the UI', () => {
  assert.equal(normalizeStatus(' Waiting_Approval '), 'waiting_approval');
  assert.equal(normalizeStatus('WAITING APPROVAL'), 'waiting_approval');
  assert.equal(normalizeStatus(null), '');
});

test('every palette entry clears WCAG AA 3:1 in both themes', () => {
  assert.ok(STATUS_DOT_CONTRAST.length >= 6);
  for (const entry of STATUS_DOT_CONTRAST) {
    assert.ok(
      entry.lightRatio >= 3,
      `${entry.tone} light contrast ${entry.lightRatio} is below 3:1`,
    );
    assert.ok(
      entry.darkRatio >= 3,
      `${entry.tone} dark contrast ${entry.darkRatio} is below 3:1`,
    );
    assert.match(entry.lightForeground, /^#[0-9a-f]{6}$/i);
    assert.match(entry.darkForeground, /^#[0-9a-f]{6}$/i);
  }
});

test('the palette covers every tone the mapping can produce', () => {
  const tones = new Set(
    [...BACKEND_STATUS_VALUES].map((status) => statusDotStyle(status).tone),
  );
  const documented = new Set(STATUS_DOT_CONTRAST.map((entry) => entry.tone));
  for (const tone of tones) {
    assert.ok(documented.has(tone), `no contrast entry for tone ${tone}`);
  }
});

/*
 * --- Contrast recomputed from the CASCADE the browser actually resolves ---
 *
 * The table in `statusDots.ts` is only trustworthy if it describes the CSS
 * that actually ships *and* the CSS the browser actually resolves for the
 * dot that is on screen.  A stylesheet can declare a colour token and still
 * paint something else: a later rule with equal-or-higher specificity wins,
 * and `var(--token)` resolves against the custom properties that are in
 * scope at that point in the cascade.
 *
 * So these helpers do not read a token and stop there.  They:
 *
 *   1. parse every rule in `frontend/web/src/style.css` into
 *      (selector, declarations, theme, source order),
 *   2. build the custom-property environment for a theme by replaying the
 *      `:root` blocks and the `prefers-color-scheme: dark` block in source
 *      order, and
 *   3. for a dot's real class list, walk every rule that matches it, sort
 *      by (specificity, source order) and take the winning declaration —
 *      which is what the browser paints.
 *
 * The measured foreground is the dot's visible colour (its border for a
 * hollow outline, its background otherwise) and the measured background is
 * the sidebar background the dot sits on, both resolved through the same
 * cascade.
 */

/** A CSS rule, flattened out of the stylesheet. */
interface CssRule {
  /** The comma-separated selector list, split. */
  selectors: string[];
  /** The declarations in the rule body, in source order. */
  declarations: Map<string, string>;
  /**
   * The theme the rule applies to.  A rule outside any media query
   * applies to both themes; only a rule inside `@media (prefers-color-scheme:
   * dark)` is dark-only.
   */
  theme: 'both' | 'light' | 'dark';
  /** The rule's position in the stylesheet; later rules win ties. */
  order: number;
}

/** Convert a CSS colour to 8-bit RGB.  Supports `#rrggbb` and `hsl(h s% l%)`. */
function cssColorToRgb(value: string): [number, number, number] {
  const text = value.trim();
  const hex = text.match(/^#([0-9a-f]{6})$/i);
  if (hex) {
    const packed = parseInt(hex[1], 16);
    return [(packed >> 16) & 255, (packed >> 8) & 255, packed & 255];
  }
  const hsl = text.match(/^hsl\(\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s*\)$/i);
  if (hsl) {
    // The standard CSS Color 3 HSL -> RGB conversion.
    const h = parseFloat(hsl[1]) / 360;
    const s = parseFloat(hsl[2]) / 100;
    const l = parseFloat(hsl[3]) / 100;
    const channel = (t: number) => {
      const k = (n: number) => (n + h * 12) % 12;
      const a = s * Math.min(l, 1 - l);
      return l - a * Math.max(-1, Math.min(k(t) - 3, 9 - k(t), 1));
    };
    return [
      Math.round(channel(0) * 255),
      Math.round(channel(8) * 255),
      Math.round(channel(4) * 255),
    ];
  }
  throw new Error(`unsupported CSS colour: ${value}`);
}

function rgbToHex(rgb: [number, number, number]): string {
  return `#${rgb.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}

/** WCAG 2.x relative luminance. */
function relativeLuminance(rgb: [number, number, number]): number {
  const [r, g, b] = rgb.map((value) => {
    const channel = value / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG 2.x contrast ratio between two opaque colours. */
function contrastRatio(
  foreground: [number, number, number],
  background: [number, number, number],
): number {
  const a = relativeLuminance(foreground);
  const b = relativeLuminance(background);
  const [high, low] = a > b ? [a, b] : [b, a];
  return (high + 0.05) / (low + 0.05);
}

/** The shipped stylesheet. */
const stylesheet = readBackend('frontend/web/src/style.css');

/**
 * The stylesheet with its comments removed.
 *
 * The comments in this file document the mapping in prose and mention class
 * names (`.status-dot.status-running`); left in place, the parser would
 * read them as selectors and "discover" rules that do not exist.
 */
const stylesheetWithoutComments = stylesheet.replace(/\/\*[\s\S]*?\*\//g, '');

/**
 * Flatten the stylesheet into rules.
 *
 * The walk tracks `@media (prefers-color-scheme: dark)` so a rule can be
 * attributed to a theme, and skips `@keyframes` bodies (their inner
 * percentages are not selectors) and other at-rules.  `order` is a single
 * counter over the whole sheet, so source order compares rules across
 * media blocks exactly as the browser does.
 */
function parseRules(): CssRule[] {
  const rules: CssRule[] = [];
  const counter = { order: 0 };
  parseInto(rules, stylesheetWithoutComments, 'both', counter);
  return rules;
}

/**
 * Walk a stylesheet (or a media block's body), appending the rules it
 * contains to `rules`.
 */
function parseInto(
  rules: CssRule[],
  source: string,
  theme: 'both' | 'light' | 'dark',
  counter: { order: number },
): void {
  let index = 0;
  while (index < source.length) {
    const braceStart = source.indexOf('{', index);
    if (braceStart < 0) break;
    const prelude = source.slice(index, braceStart).trim();

    // Find the matching close brace so a nested rule does not truncate it.
    let depth = 0;
    let braceEnd = braceStart;
    for (; braceEnd < source.length; braceEnd += 1) {
      if (source[braceEnd] === '{') depth += 1;
      else if (source[braceEnd] === '}') {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    const body = source.slice(braceStart + 1, braceEnd);

    if (prelude.startsWith('@media')) {
      // A dark media query switches the theme for everything inside it.
      const dark = /prefers-color-scheme\s*:\s*dark/.test(prelude);
      parseInto(rules, body, dark ? 'dark' : theme, counter);
    } else if (prelude.startsWith('@')) {
      // @keyframes and friends contribute no selectors we can match.
    } else if (prelude) {
      rules.push({
        selectors: prelude.split(',').map((s) => s.trim()).filter(Boolean),
        declarations: parseDeclarations(body),
        theme,
        order: counter.order++,
      });
    }
    index = braceEnd + 1;
  }
}

/** Parse a rule body into its declarations, in source order. */
function parseDeclarations(body: string): Map<string, string> {
  const declarations = new Map<string, string>();
  for (const chunk of body.split(';')) {
    const colon = chunk.indexOf(':');
    if (colon < 0) continue;
    const property = chunk.slice(0, colon).trim().toLowerCase();
    const value = chunk.slice(colon + 1).trim();
    if (property) declarations.set(property, value);
  }
  return declarations;
}

const allRules = parseRules();

/**
 * The custom-property environment for a theme.
 *
 * The `:root` blocks are replayed in source order, and the dark theme
 * additionally replays the declarations inside `@media (prefers-color-scheme:
 * dark)`.  A later block overrides an earlier one, exactly as the browser
 * does.  Note that the *final* `:root` block in this stylesheet is the light
 * Codex layer and does not redefine the `--status-*` tokens, so light mode
 * keeps the base `:root` values — which is what the browser does too.
 */
function customProperties(theme: 'light' | 'dark'): Map<string, string> {
  const properties = new Map<string, string>();
  const apply = (declarations: Map<string, string>) => {
    for (const [property, value] of declarations) {
      if (property.startsWith('--')) properties.set(property, value);
    }
  };
  for (const rule of allRules) {
    if (rule.theme !== 'both' && rule.theme !== theme) continue;
    // `:root` blocks carry the theme's custom properties.  In the dark
    // theme the tokens live in a `:root` inside the dark media query.
    if (rule.selectors.some((s) => s === ':root')) apply(rule.declarations);
  }
  return properties;
}

/**
 * Resolve `var(--name, fallback)` references against the custom properties.
 *
 * A reference can be the whole value (`background: var(--status-running)`)
 * or one part of it (`border: 2px solid var(--status-pending)`), and a
 * custom property can itself point at another one
 * (`--status-neutral: var(--status-cancelled)`), so the substitution runs
 * until it reaches a literal.
 */
function resolveValue(
  value: string,
  properties: Map<string, string>,
  seen: Set<string> = new Set(),
): string {
  return value.replace(
    /var\(\s*(--[a-z0-9-]+)\s*(?:,\s*([^)]*))?\)/gi,
    (whole, name: string, fallback: string | undefined) => {
      if (seen.has(name)) return whole;
      const resolved = properties.get(name);
      if (resolved === undefined) {
        return fallback !== undefined ? resolveValue(fallback, properties, seen) : whole;
      }
      const nextSeen = new Set(seen);
      nextSeen.add(name);
      return resolveValue(resolved, properties, nextSeen);
    },
  ).trim();
}

/**
 * Pull the colour out of a CSS value.
 *
 * A `border` shorthand such as `2px solid hsl(210 12% 38%)` carries the
 * colour after the width and the line style, so the colour cannot be read
 * from the front of the string.  Returns `null` when the value names no
 * colour at all (`none`, `transparent`, a gradient).
 */
function cssColorOf(value: string): string | null {
  const text = value.trim();
  if (text === '' || text === 'transparent' || text === 'none' || text === 'inherit') {
    return null;
  }
  if (text.includes('gradient')) return null;

  // A colour function anywhere in the value: `hsl(...)`, `rgb(...)`, and
  // their modern space-separated forms.
  const functionCall = text.match(/(?:^|\s)((?:hsl|hwb|lab|lch|oklab|oklch|rgb|rgba|hsla)\([^)]*\))/i);
  if (functionCall) return functionCall[1].trim();

  // A bare hex.
  const hex = text.match(/(?:^|\s)(#[0-9a-f]{3,8})(?=\s|$)/i);
  if (hex) return hex[1];

  // A named colour: a word that is not a keyword of the shorthand.
  const keywords = new Set([
    'solid', 'dashed', 'dotted', 'double', 'groove', 'ridge', 'inset',
    'outset', 'none', 'hidden', 'medium', 'thin', 'thick',
  ]);
  for (const token of text.split(/\s+/)) {
    if (!/^[a-z]+$/i.test(token)) continue;
    if (keywords.has(token.toLowerCase())) continue;
    return token;
  }
  return null;
}

/**
 * The specificity of a selector, as (ids, classes, elements).
 *
 * Only the selector forms this stylesheet uses are supported: type
 * selectors, class selectors, descendant combinators and the universal
 * selector.  Anything else is scored as a plain class so it cannot be
 * mistaken for something more specific than it is.
 */
function specificity(selector: string): [number, number, number] {
  const text = selector.trim();
  if (text.startsWith(':root')) return [0, 1, 0];
  let ids = 0;
  let classes = 0;
  let elements = 0;
  const withoutStrings = text.replace(/::?[\w-]+(\([^)]*\))?/g, (whole) => {
    // A pseudo-class counts as a class; a pseudo-element counts as an
    // element.
    if (whole.startsWith('::')) { elements += 1; return ''; }
    classes += 1;
    return '';
  });
  const idMatches = withoutStrings.match(/#[A-Za-z0-9_-]+/g);
  if (idMatches) ids += idMatches.length;
  const classMatches = withoutStrings.match(/\.[A-Za-z0-9_-]+/g);
  if (classMatches) classes += classMatches.length;
  const typeMatches = withoutStrings.match(/(^|[\s>+~])([A-Za-z][A-Za-z0-9]*)/g);
  if (typeMatches) elements += typeMatches.length;
  return [ids, classes, elements];
}

/**
 * Does a selector match an element with the given class list?
 *
 * The element is matched on its class list only, so a selector that
 * requires an ancestor (`.codex-workbench .status-dot`) is handled by
 * requiring every class in every compound selector to be present.
 */
function selectorMatches(selector: string, classes: Set<string>): boolean {
  const text = selector.trim();
  if (text === '*') return true;
  // A pseudo-element selector (`::after`) styles a generated child, not the
  // element itself, so it never contributes to the element's own
  // background or border.
  if (text.includes('::')) return false;
  // Split on descendant combinators; every compound part must match.
  const compounds = text.split(/\s+/).filter(Boolean);
  return compounds.every((compound) => {
    if (compound.startsWith(':root')) return false;
    const classNames = compound.match(/\.[A-Za-z0-9_-]+/g);
    if (!classNames) {
      // A type selector (e.g. `span`) cannot be checked from a class list
      // alone, so it does not contribute a match on its own.
      return false;
    }
    return classNames.every((name) => classes.has(name.slice(1)));
  });
}

/**
 * The declarations the browser would apply to an element with these
 * classes, in a given theme, after the cascade.
 */
function resolvedDeclarations(
  classes: string[],
  theme: 'light' | 'dark',
): Map<string, string> {
  const classSet = new Set(classes);
  const winners = new Map<string, {
    specificity: [number, number, number];
    order: number;
    value: string;
  }>();

  for (const rule of allRules) {
    if (rule.theme !== 'both' && rule.theme !== theme) continue;

    // The rule's most specific matching selector decides how specific the
    // whole rule is.
    let ruleSpec: [number, number, number] | null = null;
    for (const selector of rule.selectors) {
      if (!selectorMatches(selector, classSet)) continue;
      const spec = specificity(selector);
      if (!ruleSpec || compareSpecificity(spec, ruleSpec) > 0) ruleSpec = spec;
    }
    if (!ruleSpec) continue;

    for (const [property, value] of rule.declarations) {
      const seen = winners.get(property);
      if (!seen) {
        winners.set(property, { specificity: ruleSpec, order: rule.order, value });
        continue;
      }
      // The cascade: a more specific rule wins; at equal specificity the
      // rule that comes later in the stylesheet wins.
      const comparison = compareSpecificity(ruleSpec, seen.specificity);
      if (comparison > 0 || (comparison === 0 && rule.order >= seen.order)) {
        winners.set(property, { specificity: ruleSpec, order: rule.order, value });
      }
    }
  }

  const properties = customProperties(theme);
  const resolved = new Map<string, string>();
  for (const [property, winner] of winners) {
    resolved.set(property, resolveValue(winner.value, properties));
  }
  return resolved;
}

/** Compare two specificity tuples; positive means `a` is more specific. */
function compareSpecificity(
  a: [number, number, number],
  b: [number, number, number],
): number {
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return 0;
}

/**
 * The colour the dot actually paints, and the background it sits on.
 *
 * For a hollow outline the visible colour is the border; otherwise it is
 * the background.  `transparent` means the element behind it shows through,
 * so it is not a foreground colour.
 */
function renderedDotColours(
  classes: string[],
  theme: 'light' | 'dark',
): { foreground: string; background: string } {
  const declarations = resolvedDeclarations(classes, theme);
  const properties = customProperties(theme);

  const background = resolveValue(
    declarations.get('background') ?? 'transparent',
    properties,
  );
  const border = resolveValue(
    declarations.get('border') ?? declarations.get('border-color') ?? 'transparent',
    properties,
  );

  // A hollow dot paints a ring: the border is the visible foreground.  The
  // width may arrive as its own longhand or inside the `border` shorthand.
  const borderWidth = resolveValue(
    declarations.get('border-width') ?? border,
    properties,
  );
  const hollow = /(?:^|\s)([2-9]|[1-9]\d)(?:\.\d+)?px(?:\s|$)/.test(borderWidth);

  const foreground = hollow ? cssColorOf(border) : cssColorOf(background);
  if (!foreground) {
    throw new Error(
      `the dot for [${classes.join(' ')}] in the ${theme} theme has no visible colour `
      + `(background: ${background}, border: ${border})`,
    );
  }
  return { foreground, background: cssColorOf(background) ?? background };
}

/**
 * The sidebar background the dots sit on, resolved through the cascade.
 */
function renderedSidebarBackground(theme: 'light' | 'dark'): string {
  const declarations = resolvedDeclarations(['codex-sidebar'], theme);
  const properties = customProperties(theme);
  const background = resolveValue(
    declarations.get('background') ?? 'transparent',
    properties,
  );
  return cssColorOf(background) ?? background;
}

/** The dot classes the shared mapping emits for a status, for a given site. */
function dotClassesFor(status: string, site: 'sidebar' | 'panel' | 'queue'): string[] {
  const style = statusDotStyle(status);
  const classes = statusDotClassName(status).split(/\s+/).filter(Boolean);
  if (site === 'panel') {
    // The Run status panel also carries `phase-<status>` and a connection
    // class.  `phase-*` carries no colour of its own any more, but the
    // connection class does, so it is part of the element that is rendered.
    classes.push(`phase-${normalizeStatus(status)}`);
    classes.push('status-panel-dot');
    classes.push('connection-connected');
  } else if (site === 'sidebar') {
    classes.push('codex-sidebar');
  }
  assert.ok(style.colorVar.startsWith('--status-'));
  return classes;
}

test('the contrast table is recomputed from the shipped stylesheet', () => {
  for (const entry of STATUS_DOT_CONTRAST) {
    // The classes the sidebar dot really renders for this tone.
    const classes = dotClassesFor(entry.tone, 'sidebar');
    const light = renderedDotColours(classes, 'light');
    const dark = renderedDotColours(classes, 'dark');
    const lightBackground = renderedSidebarBackground('light');
    const darkBackground = renderedSidebarBackground('dark');

    const lightForeground = rgbToHex(cssColorToRgb(light.foreground));
    const darkForeground = rgbToHex(cssColorToRgb(dark.foreground));

    assert.equal(
      entry.lightBackground.toLowerCase(), lightBackground.toLowerCase(),
      `${entry.tone}: the table's light background is not the resolved sidebar background`,
    );
    assert.equal(
      entry.darkBackground.toLowerCase(), darkBackground.toLowerCase(),
      `${entry.tone}: the table's dark background is not the resolved sidebar background`,
    );
    assert.equal(
      entry.lightForeground.toLowerCase(), lightForeground.toLowerCase(),
      `${entry.tone}: the table's light foreground (${entry.lightForeground}) is not the resolved dot colour (${lightForeground})`,
    );
    assert.equal(
      entry.darkForeground.toLowerCase(), darkForeground.toLowerCase(),
      `${entry.tone}: the table's dark foreground (${entry.darkForeground}) is not the resolved dot colour (${darkForeground})`,
    );

    const lightRatio = contrastRatio(
      cssColorToRgb(light.foreground),
      cssColorToRgb(lightBackground),
    );
    const darkRatio = contrastRatio(
      cssColorToRgb(dark.foreground),
      cssColorToRgb(darkBackground),
    );
    assert.ok(
      Math.abs(lightRatio - entry.lightRatio) < 0.01,
      `${entry.tone}: table says ${entry.lightRatio} but the resolved cascade gives ${lightRatio.toFixed(2)}`,
    );
    assert.ok(
      Math.abs(darkRatio - entry.darkRatio) < 0.01,
      `${entry.tone}: table says ${entry.darkRatio} but the resolved cascade gives ${darkRatio.toFixed(2)}`,
    );

    // The acceptance criterion itself, recomputed rather than asserted.
    assert.ok(
      lightRatio >= 3,
      `${entry.tone} light contrast is ${lightRatio.toFixed(2)}, below WCAG AA 3:1`,
    );
    assert.ok(
      darkRatio >= 3,
      `${entry.tone} dark contrast is ${darkRatio.toFixed(2)}, below WCAG AA 3:1`,
    );
  }
});

test('every backend status clears 3:1 on the sidebar in both themes', () => {
  // Not just the six tones: every status the backend can emit, rendered at
  // every site that renders a dot, must be legible against the sidebar.
  const statuses = new Set<string>(BACKEND_STATUS_VALUES);
  for (const word of backendRunStatusWords()) statuses.add(word);

  for (const status of statuses) {
    for (const site of ['sidebar', 'panel', 'queue'] as const) {
      const classes = dotClassesFor(status, site);
      for (const theme of ['light', 'dark'] as const) {
        const { foreground } = renderedDotColours(classes, theme);
        const background = renderedSidebarBackground(theme);
        const ratio = contrastRatio(
          cssColorToRgb(foreground),
          cssColorToRgb(background),
        );
        assert.ok(
          ratio >= 3,
          `${status} (${site}, ${theme}) resolves to ${foreground} on ${background}: ${ratio.toFixed(2)}:1, below WCAG AA 3:1`,
        );
      }
    }
  }
});

test('the three render sites resolve to the same colour for the same status', () => {
  // One shared mapping means one colour: the sidebar task list, the Run
  // status panel and the queue view must resolve identically.
  for (const status of BACKEND_STATUS_VALUES) {
    const sidebar = renderedDotColours(dotClassesFor(status, 'sidebar'), 'light');
    const panel = renderedDotColours(dotClassesFor(status, 'panel'), 'light');
    const queue = renderedDotColours(dotClassesFor(status, 'queue'), 'light');
    assert.equal(
      rgbToHex(cssColorToRgb(panel.foreground)),
      rgbToHex(cssColorToRgb(sidebar.foreground)),
      `${status}: the Run status panel resolves to a different colour than the sidebar`,
    );
    assert.equal(
      rgbToHex(cssColorToRgb(queue.foreground)),
      rgbToHex(cssColorToRgb(sidebar.foreground)),
      `${status}: the queue view resolves to a different colour than the sidebar`,
    );
  }
});

test('no per-component rule overrides a dot colour outside the shared mapping', () => {
  // Any rule that paints a dot background or border must be keyed on the
  // shared mapping's tone class or its shape class — never on a per-status
  // or per-phase class, which is how `phase-stopping` used to paint the Run
  // status panel's dot amber while the mapping said grey.
  // The class names the shared mapping actually emits: `status-<tone>` and
  // `status-shape-<shape>`.
  const toneClasses = new Set(
    BACKEND_STATUS_VALUES.map((status) => `status-${statusDotStyle(status).tone}`),
  );
  const shapeClasses = new Set(
    BACKEND_STATUS_VALUES.map((status) => `status-shape-${statusDotStyle(status).shape}`),
  );
  const allowed = new Set<string>([
    'status-dot',
    'status-panel-dot',
    ...toneClasses,
    ...shapeClasses,
  ]);

  for (const rule of allRules) {
    const paints = rule.declarations.has('background')
      || rule.declarations.has('border')
      || rule.declarations.has('border-color')
      || rule.declarations.has('background-color');
    if (!paints) continue;
    for (const selector of rule.selectors) {
      if (!/\.status-dot|\.status-panel-dot/.test(selector)) continue;
      // A rule that targets the dot by a tone or shape class is the mapping
      // speaking; anything else is a parallel palette.
      const classNames = (selector.match(/\.[A-Za-z0-9_-]+/g) ?? []).map((n) => n.slice(1));
      const offMapping = classNames.filter(
        (name) => !allowed.has(name)
          && !name.startsWith('phase-')
          && !name.startsWith('connection-'),
      );
      const perStatus = classNames.filter(
        (name) => name.startsWith('status-')
          && !allowed.has(name)
          && !toneClasses.has(name)
          && !shapeClasses.has(name),
      );
      assert.deepEqual(
        offMapping, [],
        `${selector} paints a dot outside the shared mapping (classes: ${classNames.join(' ')})`,
      );
      assert.deepEqual(
        perStatus, [],
        `${selector} paints a dot with a per-status class that the shared mapping does not define`,
      );
    }
  }
});

test('the shipped sidebar background is the one the table measures against', () => {
  const light = renderedSidebarBackground('light');
  const dark = renderedSidebarBackground('dark');
  assert.equal(light.toLowerCase(), '#fcfcfc');
  assert.equal(dark.toLowerCase(), '#171717');
});
