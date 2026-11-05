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

/** Pull every lowercase status word the backend can write for a run. */
function backendRunStatusWords(): Set<string> {
  const words = new Set<string>();
  const sources = [
    'src/lh_harness/supervisor/lifecycle.py',
    'src/lh_harness/supervisor/service.py',
    'src/lh_harness/webapi/snapshot.py',
    'src/lh_harness/queue.py',
    'src/lh_harness/pg_queue.py',
  ];
  // Canonical lifecycle statuses, aliases, and the ACTIVE/TERMINAL sets.
  const lifecycle = readBackend('src/lh_harness/supervisor/lifecycle.py');
  for (const match of lifecycle.matchAll(/"([a-z_]+)"/g)) words.add(match[1]);
  // Status words the queue store and the web API write.
  for (const file of sources.slice(2)) {
    const text = readBackend(file);
    for (const match of text.matchAll(/["'](?:status|statuses)["']\s*[:=]\s*["']([a-z_]+)["']/g)) {
      words.add(match[1]);
    }
    for (const match of text.matchAll(/["']status["']\s*:\s*["']([a-z_]+)["']/g)) {
      words.add(match[1]);
    }
  }
  return words;
}

test('every backend run status has a dot style', () => {
  const backendWords = backendRunStatusWords();
  // Only statuses that describe a run's lifecycle are in scope; unrelated
  // words that happen to be quoted in the same modules ("status", "name",
  // "id", "resume_epoch", ...) are filtered out.
  const nonStatusWords = new Set([
    'status', 'statuses', 'name', 'id', 'control', 'resume_epoch', '_',
  ]);
  const scope = [...backendWords].filter((word) => !nonStatusWords.has(word));
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
