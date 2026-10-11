import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { ApiError, type ModelChoice, type WebMeta } from '../src/api';
import type { AdminAuditEntry } from '../src/adminApi';
import {
  browserQueueApi,
  capacityPayloadFromEdits,
  initialQueueSettingsState,
  loadQueueSettings,
  queueAuditEntries,
  queueSettingsReducer,
  QueueSettingsView,
  refreshQueueModels,
  saveQueueCapacity,
  saveQueueTrios,
  trioEditsFromRows,
  triosPayloadFromEdits,
  type QueueAction,
  type QueueApi,
  type QueueSettingsState,
  type QueueTrio,
  type QueueTrioEdit,
} from '../src/AdminApp';

// These tests never touch the network: every model-list/API call goes through
// an injected QueueApi stub, or (for browserQueueApi itself) a replaced
// globalThis.fetch that is restored when the test ends.

function fakeMeta(models: Record<string, ModelChoice[]> = {}, agents: string[] = []): WebMeta {
  return {
    service: 'lh-harness',
    api_version: '1',
    protocol_version: '1',
    capabilities: {},
    endpoint: 'test',
    models,
    agents: agents.map((id) => ({ id })),
  };
}

const LIVE_META = fakeMeta(
  {
    claude_code: [
      { id: 'glm-5.2', label: 'GLM 5.2' },
      { id: 'glm-5.3' },
    ],
    openai: [{ id: 'gpt-5' }],
  },
  ['claude_code', 'openai'],
);

const TRIOS: QueueTrio[] = [
  {
    name: 'glm',
    agent: 'claude_code',
    model: 'glm-5.2',
    mcp_profile: null,
    enabled: true,
    updated_by: 'paxton@example.com',
    updated_at: '2026-10-11T10:00:00Z',
  },
];

const AUDIT: AdminAuditEntry[] = [
  { id: 1, at: '2026-10-11 10:00', actor: 'paxton@example.com', action: 'set', name: 'queue_trio:glm', detail: {} },
  { id: 2, at: '2026-10-11 10:05', actor: 'paxton@example.com', action: 'set', name: 'queue_capacity:glm', detail: {} },
  { id: 3, at: '2026-10-11 10:01', actor: 'paxton@example.com', action: 'edit', name: 'queue_entry:2f9ab1', detail: {} },
  { id: 4, at: '2026-10-11 09:58', actor: 'paxton@example.com', action: 'set', name: 'LH_HARNESS_WEB_TOKEN', detail: {} },
];

interface StubOptions {
  trios?: QueueTrio[];
  capacity?: Record<string, number>;
  meta?: WebMeta;
  refreshMeta?: WebMeta;
  audit?: AdminAuditEntry[];
  fetchError?: unknown;
  saveTriosError?: unknown;
  saveCapacityError?: unknown;
  refreshError?: unknown;
}

interface StubRecord {
  triosPayloads: unknown[][];
  capacityPayloads: Array<Record<string, number>>;
  refreshCalls: number;
}

function stubApi(options: StubOptions = {}): { api: QueueApi; rec: StubRecord } {
  const rec: StubRecord = { triosPayloads: [], capacityPayloads: [], refreshCalls: 0 };
  const api: QueueApi = {
    queueMeta: async () => {
      if (options.fetchError) throw options.fetchError;
      return options.meta || LIVE_META;
    },
    refreshModels: async () => {
      if (options.refreshError) throw options.refreshError;
      rec.refreshCalls += 1;
      return options.refreshMeta || options.meta || LIVE_META;
    },
    fetchQueueTrios: async () => {
      if (options.fetchError) throw options.fetchError;
      return { trios: options.trios || TRIOS };
    },
    saveQueueTrios: async (trios) => {
      if (options.saveTriosError) throw options.saveTriosError;
      rec.triosPayloads.push(trios);
      return { trios: trios as QueueTrio[] };
    },
    fetchQueueCapacity: async () => {
      if (options.fetchError) throw options.fetchError;
      return { capacity: options.capacity || { glm: 2 } };
    },
    saveQueueCapacity: async (capacity) => {
      if (options.saveCapacityError) throw options.saveCapacityError;
      rec.capacityPayloads.push(capacity);
      return { capacity };
    },
    fetchQueueAudit: async () => {
      if (options.fetchError) throw options.fetchError;
      return { entries: options.audit || AUDIT };
    },
  };
  return { api, rec };
}

function recorder(): { actions: QueueAction[]; dispatch: (action: QueueAction) => void } {
  const actions: QueueAction[] = [];
  return { actions, dispatch: (action) => actions.push(action) };
}

function run(state: QueueSettingsState, actions: QueueAction[]): QueueSettingsState {
  return actions.reduce(queueSettingsReducer, state);
}

function readyState(partial: Partial<QueueSettingsState> = {}): QueueSettingsState {
  return { ...initialQueueSettingsState(), phase: 'ready', ...partial };
}

function render(state: QueueSettingsState): string {
  return renderToStaticMarkup(
    <QueueSettingsView
      state={state}
      onShowAudit={() => undefined}
      onRefreshModels={() => undefined}
      onEditTrio={() => undefined}
      onEditCapacity={() => undefined}
      onSaveTrios={() => undefined}
      onSaveCapacity={() => undefined}
    />,
  );
}

async function loadedState(api: QueueApi): Promise<QueueSettingsState> {
  const { actions, dispatch } = recorder();
  await loadQueueSettings(api, dispatch);
  return run(initialQueueSettingsState(), actions);
}

test('queue settings load: model dropdown populated from the live endpoint, grouped by provider', async () => {
  const state = await loadedState(stubApi().api);
  assert.equal(state.phase, 'ready');
  assert.deepEqual(state.agents, ['claude_code', 'openai']);
  assert.deepEqual(state.capacityEdits, { glm: '2' });

  const html = render(state);
  assert.match(html, /<select aria-label="glm agent"/);
  assert.match(html, /<select aria-label="glm model"/);
  // Grouped by the provider/agent key the meta response supplies.
  assert.ok(html.includes('<optgroup label="claude_code">'), html);
  assert.ok(html.includes('<option value="glm-5.2"'), html);
  assert.ok(html.includes('<option value="glm-5.3"'), html);
  // Another provider's models must not leak into this trio's dropdown.
  assert.ok(!html.includes('gpt-5'), html);
  // No model list is hardcoded: a 'not in the live list' marker is the only
  // way a stored-but-unlisted id can appear (glm-5.2 is listed here).
  assert.ok(!html.includes('not in the live list'), html);
  // Capacity fields come from the capacity endpoint, not from the trio names.
  assert.match(html, /<input aria-label="glm max concurrent" type="number"/);
  // The shared audit log shows the queue changes (same store as the Audit tab).
  assert.ok(html.includes('queue_trio:glm'), html);
  assert.ok(html.includes('queue_capacity:glm'), html);
  assert.ok(html.includes('queue_entry:2f9ab1'), html);
  assert.ok(!html.includes('LH_HARNESS_WEB_TOKEN'), html);
  // Refresh button present.
  assert.ok(html.includes('data-testid="refresh-models"'), html);
});

test('a saved model absent from the live list stays selectable and is marked', async () => {
  const trios: QueueTrio[] = [{ ...TRIOS[0], model: 'glm-4.6' }];
  const state = await loadedState(stubApi({ trios }).api);
  const html = render(state);
  assert.ok(html.includes('glm-4.6 (not in the live list)'), html);
});

test('dropdown options come ONLY from the live response: an empty list leaves no model options', async () => {
  const meta = fakeMeta({}, ['claude_code']);
  const state = await loadedState(stubApi({ meta }).api);
  const html = render(state);
  assert.ok(!html.includes('<optgroup'), html);
  // The default fallback option and the saved-value option are the only ones.
  assert.ok(html.includes('(default — config.toml fallback)'), html);
  assert.ok(html.includes('glm-5.2 (not in the live list)'), html);
  assert.ok(!html.includes('glm-5.3'), html);
});

test('refresh models re-fetches the live list and updates the dropdown options', async () => {
  const refreshedMeta = fakeMeta({ claude_code: [{ id: 'glm-5.4' }] }, ['claude_code']);
  const { api, rec } = stubApi({ refreshMeta: refreshedMeta });
  const state = await loadedState(api);

  const { actions, dispatch } = recorder();
  await refreshQueueModels(api, dispatch);
  const next = run(state, actions);
  assert.equal(rec.refreshCalls, 1);
  assert.deepEqual(Object.keys(next.models), ['claude_code']);
  const html = render(next);
  assert.ok(html.includes('glm-5.4'), html);
  // The previously-saved model is no longer listed, so it renders as the
  // marked fallback option rather than a live list entry.
  assert.ok(!html.includes('glm-5.2</option>'), html);
  assert.ok(html.includes('glm-5.2 (not in the live list)'), html);
});

test('refresh models failure is surfaced without touching the dropdown state', async () => {
  const api = stubApi({ refreshError: new Error('detecting models failed: gateway down') }).api;
  const state = await loadedState(api);
  const { actions, dispatch } = recorder();
  await refreshQueueModels(api, dispatch);
  const next = run(state, actions);
  assert.equal(next.refreshing, false);
  assert.equal(next.refreshError, 'detecting models failed: gateway down');
  assert.deepEqual(Object.keys(next.models), Object.keys(state.models));
  const html = render(next);
  assert.ok(html.includes('gateway down'), html);
});

test('trio save PUTs the full edited rows (null for blanks) and shows success', async () => {
  const { api, rec } = stubApi();
  const state = await loadedState(api);
  const edits: QueueTrioEdit[] = [{ name: 'glm', agent: 'claude_code', model: 'glm-5.3', mcp_profile: '', enabled: true }];
  const { actions, dispatch } = recorder();
  await saveQueueTrios(api, edits, dispatch);
  assert.deepEqual(rec.triosPayloads, [[{ name: 'glm', agent: 'claude_code', model: 'glm-5.3', mcp_profile: null, enabled: true }]]);
  const next = run(state, actions);
  assert.equal(next.triosSaved, 'Saved.');
  assert.equal(next.triosError, null);
  // The edits re-derive from the rows the endpoint returned.
  assert.equal(next.edits[0].model, 'glm-5.3');
  const html = render(next);
  assert.ok(html.includes('data-testid="queue-trios-saved"'), html);
});

test('trio save surfaces the 422 unknown-model detail and the 503 live-list refusal', async () => {
  const edits: QueueTrioEdit[] = [{ name: 'glm', agent: 'claude_code', model: 'nope', mcp_profile: '', enabled: true }];

  const api422 = stubApi({ saveTriosError: new ApiError(422, "trio 'glm': unknown model 'nope' for agent 'claude_code' in the live model list") }).api;
  const a422 = recorder();
  await saveQueueTrios(api422, edits, a422.dispatch);
  const s422 = run(readyState(), a422.actions);
  assert.equal(s422.triosSaved, null);
  assert.equal(s422.triosError, "trio 'glm': unknown model 'nope' for agent 'claude_code' in the live model list");
  const html422 = render(s422);
  assert.ok(html422.includes('data-testid="queue-trios-error"'), html422);
  assert.ok(html422.includes('unknown model'), html422);

  const api503 = stubApi({ saveTriosError: new ApiError(503, 'the live model list is unavailable; refusing to write unvalidated trios') }).api;
  const a503 = recorder();
  await saveQueueTrios(api503, edits, a503.dispatch);
  const s503 = run(readyState(), a503.actions);
  assert.equal(s503.triosError, 'the live model list is unavailable; refusing to write unvalidated trios');
  assert.ok(render(s503).includes('live model list is unavailable'));
});

test('capacity save PUTs parsed integers and shows success', async () => {
  const { api, rec } = stubApi();
  const state = await loadedState(api);
  const { actions, dispatch } = recorder();
  await saveQueueCapacity(api, { glm: '3' }, {}, dispatch);
  assert.deepEqual(rec.capacityPayloads, [{ glm: 3 }]);
  const next = run(state, actions);
  assert.equal(next.capacitySaved, 'Saved.');
  assert.ok(render(next).includes('data-testid="queue-capacity-saved"'));
});

test('invalid capacity input is refused client-side without calling the API', async () => {
  const { api, rec } = stubApi();
  const { actions, dispatch } = recorder();
  await saveQueueCapacity(api, { glm: '-1' }, {}, dispatch);
  assert.equal(rec.capacityPayloads.length, 0);
  const next = run(readyState(), actions);
  assert.match(next.capacityError || '', /non-negative integer/);
  assert.ok(render(next).includes('data-testid="queue-capacity-error"'));
});

test('capacity save surfaces the endpoint 422 detail', async () => {
  const api = stubApi({ saveCapacityError: new ApiError(422, "capacity for unknown trio 'zzz'") }).api;
  const { actions, dispatch } = recorder();
  await saveQueueCapacity(api, { zzz: '1' }, {}, dispatch);
  const next = run(readyState(), actions);
  assert.equal(next.capacityError, "capacity for unknown trio 'zzz'");
  assert.ok(render(next).includes('unknown trio'));
});

test('non-admin is denied: a 403 renders the refusal instead of the settings tables', async () => {
  const api = stubApi({ fetchError: new ApiError(403, 'this account is not on the admin list') }).api;
  const { actions, dispatch } = recorder();
  await loadQueueSettings(api, dispatch);
  const state = run(initialQueueSettingsState(), actions);
  assert.equal(state.phase, 'refused');
  const html = render(state);
  assert.ok(html.includes('data-testid="queue-refused"'), html);
  assert.ok(html.includes('not on the admin list'), html);
  assert.ok(!html.includes('<table'), html);
  assert.ok(!html.includes('refresh-models'), html);
});

test('changing the agent clears the model so a save cannot carry a stale id', () => {
  const state: QueueSettingsState = {
    ...initialQueueSettingsState(),
    phase: 'ready',
    edits: [{ name: 'glm', agent: 'claude_code', model: 'glm-5.2', mcp_profile: '', enabled: true }],
  };
  const next = queueSettingsReducer(state, { type: 'edit-trio', index: 0, patch: { agent: 'openai' } });
  assert.equal(next.edits[0].agent, 'openai');
  assert.equal(next.edits[0].model, '');
  // Editing other fields keeps the model.
  const kept = queueSettingsReducer(state, { type: 'edit-trio', index: 0, patch: { mcp_profile: 'dev' } });
  assert.equal(kept.edits[0].model, 'glm-5.2');
});

test('pure payload builders match the endpoint contracts', () => {
  assert.deepEqual(
    triosPayloadFromEdits([{ name: 'glm', agent: 'claude_code', model: ' glm-5.3 ', mcp_profile: ' dev ', enabled: false }]),
    [{ name: 'glm', agent: 'claude_code', model: 'glm-5.3', mcp_profile: 'dev', enabled: false }],
  );
  assert.deepEqual(
    trioEditsFromRows([{ name: 'glm', agent: 'claude_code', model: null, mcp_profile: null, enabled: true }]),
    [{ name: 'glm', agent: 'claude_code', model: '', mcp_profile: '', enabled: true }],
  );
  assert.deepEqual(capacityPayloadFromEdits({ glm: '2', kimi: '0' }), { capacity: { glm: 2, kimi: 0 } });
  assert.deepEqual(capacityPayloadFromEdits({ glm: '1.5' }), { error: "capacity for 'glm' must be a non-negative integer" });
  assert.deepEqual(queueAuditEntries(AUDIT, 10).map((entry) => entry.name), ['queue_trio:glm', 'queue_capacity:glm', 'queue_entry:2f9ab1']);
  assert.deepEqual(queueAuditEntries(AUDIT, 1).map((entry) => entry.name), ['queue_trio:glm']);
});

test('browserQueueApi hits the R4/meta endpoints with the admin-write guard and detail extraction', async () => {
  const calls: Array<{ url: string; init?: { method?: string; headers?: Record<string, string>; body?: unknown } }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: unknown, init?: { method?: string; headers?: Record<string, string>; body?: unknown }) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();
    calls.push({ url, init: { method, headers: init?.headers, body: init?.body } });
    const route = `${method} ${url}`;
    if (route === 'GET /api/queue/trios') return fakeJsonResponse(200, { trios: TRIOS });
    if (route === 'GET /api/queue/capacity') return fakeJsonResponse(200, { capacity: { glm: 2 } });
    if (route === 'GET /api/meta') return fakeJsonResponse(200, LIVE_META);
    if (route === 'PUT /api/queue/trios') return fakeJsonResponse(200, { ok: true, trios: TRIOS });
    if (route === 'PUT /api/queue/capacity') return fakeJsonResponse(200, { ok: true, capacity: { glm: 3 } });
    if (route === 'POST /api/models/refresh') return fakeJsonResponse(200, LIVE_META);
    if (route === 'GET /api/admin/audit?limit=50') return fakeJsonResponse(200, { entries: AUDIT });
    return fakeJsonResponse(500, { detail: 'unstubbed route' });
  }) as typeof fetch;
  try {
    const triosResult = await browserQueueApi.fetchQueueTrios();
    assert.deepEqual(triosResult.trios, TRIOS);
    const capacityResult = await browserQueueApi.fetchQueueCapacity();
    assert.deepEqual(capacityResult.capacity, { glm: 2 });
    const meta = await browserQueueApi.queueMeta();
    assert.deepEqual(Object.keys(meta.models || {}), ['claude_code', 'openai']);
    await browserQueueApi.saveQueueTrios([{ name: 'glm', agent: 'claude_code', model: 'glm-5.3', mcp_profile: null, enabled: true }]);
    await browserQueueApi.saveQueueCapacity({ glm: 3 });
    await browserQueueApi.refreshModels();
    const audit = await browserQueueApi.fetchQueueAudit(50);
    assert.deepEqual(audit.entries, AUDIT);

    const triosPut = calls.find((call) => call.url === '/api/queue/trios' && call.init?.method === 'PUT');
    assert.ok(triosPut, JSON.stringify(calls));
    assert.equal(triosPut.init?.headers?.['X-Requested-With'], 'lh-harness');
    assert.deepEqual(JSON.parse(String(triosPut.init?.body)), {
      trios: [{ name: 'glm', agent: 'claude_code', model: 'glm-5.3', mcp_profile: null, enabled: true }],
    });
    const capacityPut = calls.find((call) => call.url === '/api/queue/capacity' && call.init?.method === 'PUT');
    assert.ok(capacityPut);
    assert.equal(capacityPut.init?.headers?.['X-Requested-With'], 'lh-harness');
    assert.deepEqual(JSON.parse(String(capacityPut.init?.body)), { capacity: { glm: 3 } });
    assert.ok(calls.some((call) => call.url === '/api/models/refresh' && call.init?.method === 'POST'));
    assert.ok(calls.some((call) => call.url === '/api/meta'));
  } finally {
    globalThis.fetch = originalFetch;
  }

  // Error detail extraction: the 422/503 detail text is what the UI shows.
  globalThis.fetch = (async () => fakeJsonResponse(422, { detail: "trio 'glm': unknown model 'nope'" }, 'Unprocessable Entity')) as typeof fetch;
  try {
    await assert.rejects(
      () => browserQueueApi.saveQueueTrios([]),
      (reason: unknown) => {
        assert.ok(reason instanceof ApiError);
        assert.equal((reason as ApiError).status, 422);
        assert.equal((reason as ApiError).message, "trio 'glm': unknown model 'nope'");
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

function fakeJsonResponse(status: number, body: unknown, statusText = ''): Response {
  const make = () => ({
    ok: status >= 200 && status < 300,
    status,
    statusText,
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  const base = make() as unknown as { clone: () => Response } & Record<string, unknown>;
  base.clone = () => make() as unknown as Response;
  return base as unknown as Response;
}

test('capacity payload carries the peak companion and validates it client-side', async () => {
  const rich = capacityPayloadFromEdits({ zai: '20' }, { zai: '4' });
  assert.deepEqual(rich, { capacity: { zai: { max: 20, max_peak: 4 } } });

  const legacy = capacityPayloadFromEdits({ zai: '20' }, {});
  assert.deepEqual(legacy, { capacity: { zai: 20 } });

  const tooHigh = capacityPayloadFromEdits({ zai: '3' }, { zai: '9' });
  assert.ok('error' in tooHigh && /cannot exceed/.test(tooHigh.error));

  const notNumber = capacityPayloadFromEdits({ zai: '3' }, { zai: 'x' });
  assert.ok('error' in notNumber);
});
