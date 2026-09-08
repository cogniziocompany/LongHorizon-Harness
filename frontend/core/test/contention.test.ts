import assert from 'node:assert/strict';
import { test } from 'node:test';
import { projectStatus } from '../src/statusView';
import type { EventEnvelope, Snapshot } from '../src/types';

function event(type: string, payload: unknown, ts = 1): EventEnvelope {
  return {
    schema_version: 1,
    event_id: `e-${type}-${ts}`,
    type,
    ts,
    run_id: 'run-a',
    round: null,
    role: null,
    status: null,
    payload: payload as Record<string, unknown>,
    legacy: {},
  };
}

function baseSnapshot(events: EventEnvelope[] = []): Snapshot {
  return {
    schema_version: 1,
    run: { id: 'run-a', status: 'running', log_dir: '/tmp/run-a' },
    mission: { task: 'task', contract_path: '', plan_path: '', verified_state_path: '', report_path: '' },
    rounds: [],
    active_round: null,
    active_role: null,
    events,
    approvals: [],
    controls: { can_inject: true, can_abort: true, can_resume: false },
    diagnostics: { last_event_id: events.at(-1)?.event_id || null, event_count: events.length, warnings: [] },
  };
}

test('detected contention event is surfaced with peers', () => {
  const detected = event('fleet.contention.detected', {
    contention_id: 'c123',
    severity: 'same_repo',
    group_key: 'repo:host/org/repo',
    peers: [{ run_id: 'run-b', workspace: '/b', branch: 'main' }],
  });
  const view = projectStatus(baseSnapshot([detected]));
  assert.equal(view.contention?.contention_id, 'c123');
  assert.equal(view.contention?.severity, 'same_repo');
  assert.equal(view.contention?.peers.length, 1);
  assert.equal(view.contention?.peers[0]?.run_id, 'run-b');
});

test('cleared event does not surface contention', () => {
  const cleared = event('fleet.contention.cleared', { contention_id: 'c123', severity: 'same_repo' });
  const view = projectStatus(baseSnapshot([cleared]));
  assert.equal(view.contention, null);
});

test('malformed tier yields null contention', () => {
  const detected = event('fleet.contention.detected', {
    contention_id: 'c123',
    severity: 'bad_tier',
    peers: [{ run_id: 'run-b', workspace: '/b' }],
  });
  const view = projectStatus(baseSnapshot([detected]));
  assert.equal(view.contention, null);
});

test('malformed peers is accepted as empty array', () => {
  const detected = event('fleet.contention.detected', {
    contention_id: 'c123',
    severity: 'same_repo',
    peers: 'not-an-array',
  });
  const view = projectStatus(baseSnapshot([detected]));
  assert.equal(view.contention?.peers.length, 0);
});

test('latest detected event wins', () => {
  const first = event('fleet.contention.detected', {
    contention_id: 'first',
    severity: 'same_repo',
    peers: [{ run_id: 'run-b', workspace: '/b' }],
  }, 1);
  const second = event('fleet.contention.detected', {
    contention_id: 'second',
    severity: 'same_tree',
    peers: [{ run_id: 'run-c', workspace: '/c' }],
  }, 2);
  const view = projectStatus(baseSnapshot([first, second]));
  assert.equal(view.contention?.contention_id, 'second');
  assert.equal(view.contention?.severity, 'same_tree');
});
