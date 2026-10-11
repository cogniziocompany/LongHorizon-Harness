import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Snapshot } from '../../core/src/types';
import { UiLanguageProvider } from '../src/i18n';
import { LockedLaunchModelSection } from '../src/App';

// LHH-QUEUE-HOTRELOAD (R12): run-page display of the model/roles resolved at
// launch and locked on the run record.  These tests never touch the network:
// the section is a pure function of the run payload it is handed, rendered
// via renderToStaticMarkup with no fetch anywhere in the tree.  The payload
// fixtures mirror what build_snapshot projects from the supervisor owner
// record (the state R8 writes at launch), NOT the live meta model list or
// the current trio defaults.

function runPayload(overrides: Partial<Snapshot['run']> = {}): Snapshot['run'] {
  return {
    id: 'run-1',
    status: 'running',
    log_dir: '/workspace/run-1',
    ...overrides,
  };
}

function renderSection(run: Snapshot['run']): string {
  return renderToStaticMarkup(
    <UiLanguageProvider>
      <LockedLaunchModelSection run={run} />
    </UiLanguageProvider>,
  );
}

test('renders the recorded at-launch model, labeled locked and read-only', () => {
  const html = renderSection(runPayload({ model: 'glm-5.2' }));

  assert.ok(html.includes('Model (locked at launch)'), html);
  assert.ok(html.includes('Read-only'), html);
  assert.ok(html.includes('data-testid="locked-launch-model-value"'), html);
  assert.ok(html.includes('glm-5.2'), html);
  assert.ok(html.includes('Locked'), html);
  assert.match(html, /trio default changes do not affect this run/);
});

test('a later trio default change cannot alter the displayed model', () => {
  // The payload below is what the owner record carried at launch: trio "glm"
  // then defaulted to glm-5.2.  The operator later edits the trio default to
  // glm-9.9.  That edit lands on FUTURE launches only — the owner record of
  // this run is untouched, so the section re-rendered after the change still
  // shows glm-5.2 and never the new default.  The component takes only the
  // run record; there is no meta/trio input it could silently follow.
  const recordedAtLaunch = runPayload({ model: 'glm-5.2' });
  const before = renderSection(recordedAtLaunch);
  const afterTrioDefaultChangedToGlm99 = renderSection(recordedAtLaunch);

  assert.equal(afterTrioDefaultChangedToGlm99, before);
  assert.ok(afterTrioDefaultChangedToGlm99.includes('glm-5.2'));
  assert.ok(!afterTrioDefaultChangedToGlm99.includes('glm-9.9'), afterTrioDefaultChangedToGlm99);
});

test('provider-default model (recorded null) renders honestly, never the string "null"', () => {
  const html = renderSection(runPayload({ model: null }));

  assert.ok(html.includes('Model (locked at launch)'), html);
  assert.ok(html.includes('Agent/provider default (no model selected)'), html);
  assert.ok(!html.includes('>null<'), html);
});

test('runs without recorded launch state render nothing (no fake trio fallback, no crash)', () => {
  // Older payloads carry neither model nor role_configs.
  assert.equal(renderSection(runPayload({})), '');
  // A blank model string (defensive) also renders nothing.
  assert.equal(renderSection(runPayload({ model: '   ' })), '');
  // Nothing rendered => nothing misleading and nothing implying editability.
});

test('recorded role configs render per-role agent · model, all three roles', () => {
  const html = renderSection(runPayload({
    model: 'glm-5.2',
    role_configs: {
      manager: { agent: 'claude_code', model: 'glm-5.2' },
      executor: { agent: 'codex', model: 'gpt-5' },
      auditor: { agent: 'claude_code', model: 'kimi-k2' },
    },
  }));

  for (const role of ['manager', 'executor', 'auditor']) {
    assert.ok(html.includes(`data-testid="locked-launch-role-${role}"`), html);
  }
  assert.ok(html.includes('Manager'), html);
  assert.ok(html.includes('Executor'), html);
  assert.ok(html.includes('Auditor'), html);
  assert.ok(html.includes('claude_code · glm-5.2'), html);
  assert.ok(html.includes('codex · gpt-5'), html);
  assert.ok(html.includes('claude_code · kimi-k2'), html);
});

test('roles render even when the run-level model field is absent', () => {
  const html = renderSection(runPayload({
    role_configs: {
      manager: { agent: 'claude_code', model: 'glm-5.2' },
      executor: { agent: 'codex', model: 'gpt-5' },
      auditor: { agent: 'claude_code', model: 'kimi-k2' },
    },
  }));

  assert.ok(html.includes('Model (locked at launch)'), html);
  assert.ok(html.includes('data-testid="locked-launch-role-executor"'), html);
  // No run-level model was recorded, so no run-level model row either.
  assert.ok(!html.includes('data-testid="locked-launch-model-value"'), html);
});

test('malformed role entries are skipped honestly; partial records still render', () => {
  const html = renderSection(runPayload({
    role_configs: {
      manager: { agent: 'claude_code', model: 'glm-5.2' },
      // executor entry is malformed (wrong shape) — skipped, not invented.
      executor: 42 as unknown as { agent: string; model: string },
      auditor: { agent: 'claude_code', model: '  ' },
    },
  }));

  assert.ok(html.includes('data-testid="locked-launch-role-manager"'), html);
  assert.ok(!html.includes('data-testid="locked-launch-role-executor"'), html);
  // The auditor entry records an agent but no usable model — agent only.
  assert.ok(html.includes('data-testid="locked-launch-role-auditor"'), html);
  assert.ok(html.includes('<small>claude_code</small>'), html);
});

test('display is read-only: no picker, dropdown, input, or edit affordance', () => {
  const html = renderSection(runPayload({
    model: 'glm-5.2',
    role_configs: {
      manager: { agent: 'claude_code', model: 'glm-5.2' },
      executor: { agent: 'codex', model: 'gpt-5' },
      auditor: { agent: 'claude_code', model: 'kimi-k2' },
    },
  }));

  assert.ok(!/<select[\s>]/.test(html), html);
  assert.ok(!/<option[\s>]/.test(html), html);
  assert.ok(!/<input[\s>]/.test(html), html);
  assert.ok(!/<button[\s>]/.test(html), html);
});
