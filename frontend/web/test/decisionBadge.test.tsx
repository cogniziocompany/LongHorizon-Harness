import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Approval } from '../../core/src/types';
import { UiLanguageProvider } from '../src/i18n';
import { ApprovalCard, DecisionBadge, OwnerNotePanel } from '../src/App';

// LHH-GATE-OWNER-NOTE (round 4): web UX integration tests for the
// decision-maker badge + expandable owner-note panel that the run page and
// runs list render for any pending gate.  The tests are network-free: every
// component is a pure function of the approval object it receives, rendered
// via renderToStaticMarkup with no fetch anywhere in the tree.

function pendingApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    approval_id: 'apr-1',
    title: 'Continue the run?',
    message: 'The worker is asking whether to continue.',
    options: [{ value: 'continue', label: 'Continue' }, { value: 'stop', label: 'Stop', style: 'danger' }],
    answers: [],
    allow_input: true,
    input_label: 'Optional',
    context: {},
    round_index: 0,
    status: 'pending',
    action: '',
    reason: '',
    user_input: '',
    created_at: 0,
    resolved_at: null,
    ...overrides,
  };
}

function renderBadge(owner: string | undefined, compact = false): string {
  return renderToStaticMarkup(
    <UiLanguageProvider>
      <DecisionBadge owner={owner} compact={compact} />
    </UiLanguageProvider>,
  );
}

function renderPanel(note: string | undefined, setBy?: string): string {
  return renderToStaticMarkup(
    <UiLanguageProvider>
      <OwnerNotePanel note={note} setBy={setBy} />
    </UiLanguageProvider>,
  );
}

function renderCard(approval: Approval): string {
  return renderToStaticMarkup(
    <UiLanguageProvider>
      <ApprovalCard
        approval={approval}
        busy={false}
        userInput=""
        onUserInput={() => {}}
        extraRounds=""
        onExtraRounds={() => {}}
        onApprove={() => {}}
      />
    </UiLanguageProvider>,
  );
}

// --- DecisionBadge ------------------------------------------------------

test('DecisionBadge renders Decision: Overseer1 for owner=overseer', () => {
  const html = renderBadge('overseer');
  assert.ok(html.includes('data-testid="decision-badge"'), html);
  assert.ok(html.includes('data-owner="overseer"'), html);
  assert.ok(html.includes('Overseer1'), html);
  assert.ok(html.includes('Decision:'), html);
});

test('DecisionBadge renders Decision: Paxton for owner=paxton', () => {
  const html = renderBadge('paxton');
  assert.ok(html.includes('data-owner="paxton"'), html);
  assert.ok(html.includes('>Paxton<'), html);
});

test('DecisionBadge renders Decision: requester for owner=requester', () => {
  const html = renderBadge('requester');
  assert.ok(html.includes('>requester<'), html);
});

test('DecisionBadge renders nothing when owner is missing', () => {
  assert.equal(renderBadge(undefined), '');
  assert.equal(renderBadge(''), '');
});

test('DecisionBadge compact variant hides the Decision: prefix in visible text', () => {
  const html = renderBadge('overseer', true);
  assert.ok(html.includes('data-testid="decision-badge"'), html);
  assert.ok(html.includes('decision-badge-compact'), html);
  // The visible "Decision: " label is hidden in compact mode, but the
  // aria-label still surfaces "Decision: Overseer1" for screen readers —
  // so assert against the visible <span class="decision-badge-key"> only.
  assert.match(html, /<span class="decision-badge-key"><\/span>/, html);
  assert.ok(html.includes('Overseer1'), html);
});

// --- OwnerNotePanel -----------------------------------------------------

test('OwnerNotePanel hides itself entirely when note is empty/whitespace', () => {
  assert.equal(renderPanel(undefined), '');
  assert.equal(renderPanel(''), '');
  assert.equal(renderPanel('   '), '');
  assert.ok(!renderPanel('  ').includes('data-testid="owner-note-panel"'));
});

test('OwnerNotePanel renders the description toggle and body when note is present', () => {
  const html = renderPanel('I approve these gates, not you.');
  assert.ok(html.includes('data-testid="owner-note-panel"'), html);
  assert.ok(html.includes('data-testid="owner-note-body"'), html);
  assert.ok(html.includes('>Description<'), html);
  assert.ok(html.includes('I approve these gates, not you.'), html);
});

test('OwnerNotePanel renders clickable links for https URLs in the note', () => {
  const html = renderPanel('See https://example.com/path for the file.');
  assert.match(html, /href="https:\/\/example\.com\/path"/, html);
  assert.match(html, /target="_blank"/, html);
  assert.match(html, /rel="noreferrer"/, html);
});

test('OwnerNotePanel renders markdown bullets as <ul>/<li>', () => {
  const html = renderPanel('- first item\n- second item');
  assert.ok(html.includes('<ul>'), html);
  // Each <li> wraps its content in a <span> so links/inline markdown still
  // resolve — assert the items are rendered (not the exact DOM shape).
  assert.match(html, /<li>[\s\S]*?first item[\s\S]*?<\/li>/, html);
  assert.match(html, /<li>[\s\S]*?second item[\s\S]*?<\/li>/, html);
});

test('OwnerNotePanel renders the writer label when setBy is provided', () => {
  const html = renderPanel('A note.', 'Overseer1');
  assert.ok(html.includes('Overseer1'), html);
  assert.ok(html.includes('owner-note-author'), html);
});

test('OwnerNotePanel omits the writer label when setBy is empty', () => {
  const html = renderPanel('A note.', '');
  assert.ok(!html.includes('owner-note-author'), html);
});

// --- ApprovalCard integration ------------------------------------------

test('ApprovalCard renders the Decision badge on the run page when owner is set', () => {
  const html = renderCard(pendingApproval({ owner: 'overseer' }));
  assert.ok(html.includes('data-testid="decision-badge"'), html);
  assert.ok(html.includes('data-owner="overseer"'), html);
  assert.ok(html.includes('Overseer1'), html);
  // The badge lives inside the message-meta of the approval card.
  assert.ok(html.includes('<div class="message-meta">'), html);
  assert.match(
    html,
    /<div class="message-meta">[\s\S]*data-testid="decision-badge"[\s\S]*<\/div>/,
    'badge must be inside message-meta of the approval card',
  );
});

test('ApprovalCard omits the Decision badge when owner is unset', () => {
  const html = renderCard(pendingApproval({ owner: undefined }));
  assert.ok(!html.includes('data-testid="decision-badge"'), html);
});

test('ApprovalCard renders the expandable Description panel when owner_note is set', () => {
  const html = renderCard(pendingApproval({
    owner: 'overseer',
    owner_note: 'Hold: I asked the powerplatform session where build-runner-v2.js lives.',
    owner_set_by: 'Overseer1',
    owner_set_at: 1700000000,
  }));
  assert.ok(html.includes('data-testid="owner-note-panel"'), html);
  assert.ok(html.includes('data-testid="owner-note-body"'), html);
  assert.ok(html.includes('Hold: I asked the powerplatform session'), html);
  assert.ok(html.includes('>Description<'), html);
});

test('ApprovalCard hides the Description panel when owner_note is empty', () => {
  const html = renderCard(pendingApproval({
    owner: 'overseer',
    owner_note: '',
    owner_set_by: 'Overseer1',
  }));
  assert.ok(!html.includes('data-testid="owner-note-panel"'), html);
  // The badge still appears, so the operator knows who decides it even
  // without an explanation attached.
  assert.ok(html.includes('data-testid="decision-badge"'), html);
});

test('ApprovalCard with a long owner_note keeps the markdown panel hidden only when truly empty', () => {
  const html = renderCard(pendingApproval({
    owner: 'paxton',
    owner_note: '   \n   \n   ',
  }));
  assert.ok(!html.includes('data-testid="owner-note-panel"'), html);
  // And the badge is still there for "Paxton".
  assert.ok(html.includes('>Paxton<'), html);
});

test('ApprovalCard renders owner_note clickable links and bullets together', () => {
  const html = renderCard(pendingApproval({
    owner: 'any',
    owner_note: 'See https://example.com/x for context.\n\n- item one\n- item two',
  }));
  assert.match(html, /href="https:\/\/example\.com\/x"/, html);
  assert.ok(html.includes('<ul>'), html);
  assert.match(html, /<li>[\s\S]*?item one[\s\S]*?<\/li>/, html);
});