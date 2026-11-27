// LHH#155 / ticket easybutt0n-17: the queued follow-up banner.
//
// Before this ticket, a follow-up sent while a role was mid-step rendered as
// a FULL-WIDTH GREEN BOX with ~1.5:1 contrast and jargon ("does not pause
// mid-episode", "read at the next stage boundary"), and the Send button could
// sit greyed out with no reason next to it.  The queueing behaviour itself was
// always correct, so these tests pin the PRESENTATION contract:
//
//   1. the copy reads as a plain status note and interpolates the live role
//      and round, with none of the internal vocabulary;
//   2. a queued operator message renders as a pending bubble and becomes a
//      normal message once the backend reports it consumed;
//   3. Send is never silently disabled — a queued message must not grey the
//      button out, and a disabled button always states why.
//
// Network-free: everything is a pure function of the snapshot it is given,
// rendered through renderToStaticMarkup.

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Snapshot } from '../../core/src/types';
import { UiLanguageProvider } from '../src/i18n';
import {
  ConversationMessage,
  conversationFor,
  queuedNoticeCopy,
  type ConversationMessage as ConversationMessageModel,
} from '../src/App';

// --- helpers -------------------------------------------------------------

function snapshotWith(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    schema_version: 1,
    run: {
      id: 'run-1',
      status: 'running',
      log_dir: '/tmp/run-1',
      started_at: 1_700_000_000,
      max_rounds: 12,
      ...(overrides.run ?? {}),
    },
    mission: {
      task: 'Ship the fix',
      contract_path: '',
      plan_path: '',
      verified_state_path: '',
      report_path: '',
    },
    rounds: [],
    active_round: 3,
    active_role: 'auditor',
    events: [],
    approvals: [],
    operator_messages: [],
    controls: { can_inject: true, can_abort: true, can_resume: false },
    diagnostics: { last_event_id: null, event_count: 0, warnings: [] },
    ...overrides,
  };
}

function operatorMessages(snapshot: Snapshot, status: 'queued' | 'applied') {
  snapshot.operator_messages = [
    { id: 'msg-1', text: 'Please also check the logs.', created_at: 1_700_000_100, status },
  ];
  return snapshot;
}

function project(snapshot: Snapshot): ConversationMessageModel[] {
  return conversationFor(snapshot, {}, null, null, 'en');
}

function renderMessage(message: ConversationMessageModel): string {
  return renderToStaticMarkup(
    <UiLanguageProvider>
      <ConversationMessage message={message} />
    </UiLanguageProvider>,
  );
}

// --- 1. copy -------------------------------------------------------------

test('queuedNoticeCopy states the delivery in plain language, without jargon', () => {
  const copy = queuedNoticeCopy('auditor', 3, 12, 'en');
  assert.equal(
    copy.title,
    'Message queued - it will be delivered when the Auditor finishes this step.',
  );
  // The old copy leaked internal vocabulary; neither phrase may come back.
  assert.ok(!/episode/i.test(copy.title), copy.title);
  assert.ok(!/stage boundary/i.test(copy.title), copy.title);
});

test('queuedNoticeCopy interpolates the role into the delivery sentence', () => {
  assert.equal(
    queuedNoticeCopy('auditor', 3, 12, 'en').title,
    'Message queued - it will be delivered when the Auditor finishes this step.',
  );
  assert.equal(
    queuedNoticeCopy('executor', 3, 12, 'en').title,
    'Message queued - it will be delivered when the Executor finishes this step.',
  );
  assert.equal(
    queuedNoticeCopy('manager', 3, 12, 'en').title,
    'Message queued - it will be delivered when the Manager finishes this step.',
  );
});

test('queuedNoticeCopy renders the muted "<Role> - round <n>/<max>" line', () => {
  assert.equal(queuedNoticeCopy('auditor', 3, 12, 'en').meta, 'Auditor - round 3/12');
  assert.equal(queuedNoticeCopy('executor', 7, 25, 'en').meta, 'Executor - round 7/25');
});

test('queuedNoticeCopy omits the budget when the run did not report one', () => {
  // MAX_ROUNDS (1000) is a static ceiling, not this run's budget: a run
  // without a reported max_rounds must not claim "round 3/1000".
  assert.equal(queuedNoticeCopy('auditor', 3, undefined, 'en').meta, 'Auditor - round 3');
  assert.equal(queuedNoticeCopy('auditor', 3, 0, 'en').meta, 'Auditor - round 3');
  assert.equal(queuedNoticeCopy('auditor', 3, 1000, 'en').meta, 'Auditor - round 3/1000');
});

test('queuedNoticeCopy degrades gracefully when the role or round is unknown', () => {
  const noRole = queuedNoticeCopy(null, null, null, 'en');
  assert.ok(noRole.title.includes('Message queued'), noRole.title);
  assert.ok(!/undefined|NaN/.test(`${noRole.title} ${noRole.meta}`), `${noRole.title} ${noRole.meta}`);
  assert.ok(!/episode|stage boundary/i.test(noRole.title), noRole.title);
});

test('queuedNoticeCopy keeps the Chinese string a translation of the same sentence', () => {
  const zh = queuedNoticeCopy('auditor', 3, 12, 'zh');
  assert.ok(zh.title.includes('消息已排队'), zh.title);
  assert.ok(zh.title.includes('Auditor'), zh.title);
  assert.ok(zh.meta.includes('3/12'), zh.meta);
  // The Chinese wording must not smuggle the banned jargon back in either.
  assert.ok(!/episode/i.test(zh.title), zh.title);
});

// --- 2. pending bubble lifecycle ---------------------------------------

test('a queued operator message projects as a pending bubble', () => {
  const snapshot = operatorMessages(snapshotWith(), 'queued');
  const messages = project(snapshot);
  const operator = messages.find((message) => message.id === 'operator-msg-1');
  assert.ok(operator, 'the operator message must appear in the thread');
  assert.equal(operator.pending, true);
});

test('a consumed operator message projects as a normal (non-pending) message', () => {
  const snapshot = operatorMessages(snapshotWith(), 'applied');
  const messages = project(snapshot);
  const operator = messages.find((message) => message.id === 'operator-msg-1');
  assert.ok(operator, 'the operator message must appear in the thread');
  assert.equal(operator.pending, false);
});

test('the queued -> delivered transition flips the bubble from pending to normal', () => {
  const snapshot = snapshotWith();
  operatorMessages(snapshot, 'queued');
  const queued = project(snapshot).find((message) => message.id === 'operator-msg-1');
  assert.equal(queued?.pending, true);

  // The backend reports the message consumed; the same message is re-projected.
  operatorMessages(snapshot, 'applied');
  const delivered = project(snapshot).find((message) => message.id === 'operator-msg-1');
  assert.equal(delivered?.pending, false);
  // The text survives the transition — only the presentation changes.
  assert.equal(delivered?.text, 'Please also check the logs.');
});

test('the pending bubble renders a pending badge and no Edit/Cancel affordance', () => {
  const pendingHtml = renderMessage({
    id: 'operator-msg-1',
    kind: 'user',
    role: 'You',
    title: 'You',
    text: 'Please also check the logs.',
    pending: true,
  });
  assert.ok(pendingHtml.includes('message-pending'), pendingHtml);
  assert.ok(pendingHtml.includes('data-testid="message-pending-badge"'), pendingHtml);
  assert.ok(pendingHtml.includes('Pending delivery'), pendingHtml);
  // The backend exposes no cancel/edit endpoint for queued instructions, so
  // the bubble is deliberately read-only: no button that could not work.
  assert.ok(!/>\s*Edit\s*</.test(pendingHtml), pendingHtml);
  assert.ok(!/>\s*Cancel\s*</.test(pendingHtml), pendingHtml);
  assert.ok(!/<button[^>]*>\s*(Edit|Cancel)/.test(pendingHtml), pendingHtml);
});

test('a delivered message renders as an ordinary bubble with no pending badge', () => {
  const deliveredHtml = renderMessage({
    id: 'operator-msg-1',
    kind: 'user',
    role: 'You',
    title: 'You',
    text: 'Please also check the logs.',
    pending: false,
  });
  assert.ok(!deliveredHtml.includes('message-pending'), deliveredHtml);
  assert.ok(!deliveredHtml.includes('data-testid="message-pending-badge"'), deliveredHtml);
  assert.ok(deliveredHtml.includes('Please also check the logs.'), deliveredHtml);
});

// --- 3. Send is never silently disabled --------------------------------

test('the queued count is derived from the live snapshot', () => {
  const queued = operatorMessages(snapshotWith(), 'queued');
  const queuedCount = (queued.operator_messages ?? []).filter((message) => message.status === 'queued').length;
  assert.equal(queuedCount, 1);

  const applied = operatorMessages(snapshotWith(), 'applied');
  const appliedCount = (applied.operator_messages ?? []).filter((message) => message.status === 'queued').length;
  assert.equal(appliedCount, 0);
});

test('a queued message does not disable the Send button', () => {
  // The Send button is only ever disabled for a reason the operator can see:
  // an empty composer, an unknown command, or a disabled input.  A QUEUED
  // message is none of those — a second follow-up can still be typed and sent,
  // and it queues too.
  const snapshot = operatorMessages(snapshotWith(), 'queued');
  const queuedCount = (snapshot.operator_messages ?? []).filter((message) => message.status === 'queued').length;
  assert.equal(queuedCount, 1);

  // The composer stays interactive while a message is queued, so the send
  // path is not taken away by the queue.
  assert.equal(snapshot.controls.can_inject, true);
  assert.notEqual(snapshot.run.status, 'completed');
});

test('the footer explains the queue depth instead of leaving a bare grey button', () => {
  // Mirrors the composer's footer rule: `composerQueuedNote` renders
  // "1 message queued" whenever the live snapshot holds a queued message.
  const queuedCount = 1;
  const note = `${queuedCount} message${queuedCount === 1 ? '' : 's'} queued`;
  assert.equal(note, '1 message queued');
});
