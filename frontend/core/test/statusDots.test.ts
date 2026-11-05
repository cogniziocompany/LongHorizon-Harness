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
 * --- Contrast recomputed from the shipped stylesheet ---------------------
 *
 * The table in `statusDots.ts` is only trustworthy if it describes the CSS
 * that actually ships.  These helpers re-read
 * `frontend/web/src/style.css`, pull the real `--status-*` custom
 * properties out of the light `:root` block and the dark
 * `prefers-color-scheme: dark` block, pull the real `.codex-sidebar`
 * background out of the light and dark theme blocks, and recompute the
 * WCAG 2.x contrast ratios.  The test below then asserts that the table
 * matches the recomputation, so a colour change in the stylesheet cannot
 * silently leave a stale ratio in the source (or in the PR body).
 */

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

/** The body of a CSS block that starts at `anchor`, or '' if not found. */
function cssBlock(anchor: string): string {
  const start = stylesheet.indexOf(anchor);
  if (start < 0) return '';
  const open = stylesheet.indexOf('{', start);
  // Walk to the matching close brace so a nested rule does not truncate the
  // block early.
  let depth = 0;
  for (let index = open; index < stylesheet.length; index += 1) {
    const character = stylesheet[index];
    if (character === '{') depth += 1;
    else if (character === '}') {
      depth -= 1;
      if (depth === 0) return stylesheet.slice(open + 1, index);
    }
  }
  return stylesheet.slice(open + 1);
}

/** Read a custom property out of a CSS block body. */
function cssVar(block: string, name: string): string {
  const match = block.match(new RegExp(`--${name}:\\s*([^;]+);`));
  if (!match) throw new Error(`CSS variable --${name} not found`);
  return match[1].trim();
}

/** Every top-level `:root { ... }` block body in the stylesheet. */
function rootBlocks(): string[] {
  const bodies: string[] = [];
  const pattern = /:root\s*\{/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(stylesheet)) !== null) {
    const open = stylesheet.indexOf('{', match.index);
    let depth = 0;
    for (let index = open; index < stylesheet.length; index += 1) {
      if (stylesheet[index] === '{') depth += 1;
      else if (stylesheet[index] === '}') {
        depth -= 1;
        if (depth === 0) {
          bodies.push(stylesheet.slice(open + 1, index));
          pattern.lastIndex = index;
          break;
        }
      }
    }
  }
  return bodies;
}

/**
 * The light palette lives in the `:root { ... }` block that declares the
 * `--status-*` tokens; the dark palette overrides them inside
 * `@media (prefers-color-scheme: dark)`.
 */
const lightPalette = (() => {
  for (const block of rootBlocks()) {
    if (/--status-running\s*:/.test(block)) return block;
  }
  throw new Error('could not find the :root block that declares --status-running');
})();
const darkPalette = cssBlock('@media (prefers-color-scheme: dark)');

// The sidebar backgrounds the dots sit on.  The stylesheet paints
// `.codex-sidebar` three times: the dark theme paints it with
// `var(--codex-sidebar)` (#171717) and the final light layer paints it
// literally (#fcfcfc).  Both values are read back out of the sheet.
const darkSidebar = cssVar(cssBlock(':root { --codex-bg'), 'codex-sidebar');
const lightSidebar = (() => {
  // The light layer is the last `:root` block that redefines the sidebar.
  for (const block of rootBlocks().reverse()) {
    const value = block.match(/--codex-sidebar:\s*([^;]+);/);
    if (value && value[1].trim() !== darkSidebar) return value[1].trim();
  }
  throw new Error('could not find the light --codex-sidebar');
})();

test('the contrast table is recomputed from the shipped stylesheet', () => {
  for (const entry of STATUS_DOT_CONTRAST) {
    const lightCss = cssVar(lightPalette, `status-${entry.tone}`);
    const darkCss = cssVar(darkPalette, `status-${entry.tone}`);

    assert.equal(
      entry.lightCssValue, lightCss,
      `${entry.tone}: the table's light CSS value does not match the stylesheet`,
    );
    assert.equal(
      entry.darkCssValue, darkCss,
      `${entry.tone}: the table's dark CSS value does not match the stylesheet`,
    );

    const lightForeground = rgbToHex(cssColorToRgb(lightCss));
    const darkForeground = rgbToHex(cssColorToRgb(darkCss));
    assert.equal(
      entry.lightForeground, lightForeground,
      `${entry.tone}: the table's light foreground does not match the stylesheet`,
    );
    assert.equal(
      entry.darkForeground, darkForeground,
      `${entry.tone}: the table's dark foreground does not match the stylesheet`,
    );

    assert.equal(
      entry.lightBackground, lightSidebar,
      `${entry.tone}: the table's light background is not the shipped sidebar background`,
    );
    assert.equal(
      entry.darkBackground, darkSidebar,
      `${entry.tone}: the table's dark background is not the shipped sidebar background`,
    );

    const lightRatio = contrastRatio(
      cssColorToRgb(lightCss),
      cssColorToRgb(lightSidebar),
    );
    const darkRatio = contrastRatio(
      cssColorToRgb(darkCss),
      cssColorToRgb(darkSidebar),
    );
    assert.ok(
      Math.abs(lightRatio - entry.lightRatio) < 0.01,
      `${entry.tone}: table says ${entry.lightRatio} but the stylesheet gives ${lightRatio.toFixed(2)}`,
    );
    assert.ok(
      Math.abs(darkRatio - entry.darkRatio) < 0.01,
      `${entry.tone}: table says ${entry.darkRatio} but the stylesheet gives ${darkRatio.toFixed(2)}`,
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

test('the shipped sidebar background is the one the table measures against', () => {
  assert.equal(lightSidebar, '#fcfcfc');
  assert.equal(darkSidebar, '#171717');
  // The dots really are painted on that background: the dark theme paints
  // `.codex-sidebar` with the --codex-sidebar variable, and the final light
  // layer paints it with the same literal value.
  assert.match(stylesheet, /\.codex-sidebar\s*\{[^}]*background:\s*var\(--codex-sidebar\)/);
  assert.match(stylesheet, /\.codex-sidebar\s*\{[^}]*background:\s*#fcfcfc/);
});
