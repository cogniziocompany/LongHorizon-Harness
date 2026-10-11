import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { ApiError, type WebMeta } from '../src/api';
import { UiLanguageProvider } from '../src/i18n';
import {
  browserQueueTasksApi,
  buildQueueRolesPayload,
  clearQueueTaskRoles,
  initialQueueTasksState,
  loadQueueTasks,
  modelOptionsFromMeta,
  queueTasksReducer,
  QueueTasksDrawer,
  rolePinsFromSelection,
  saveQueueTaskDraft,
  type QueueRoleDrafts,
  type QueueTaskEntry,
  type QueueTasksAction,
  type QueueTasksApi,
  type QueueTasksState,
} from '../src/App';

// These tests never touch the network: every model-list/API call goes through
// an injected QueueTasksApi stub, or (for browserQueueTasksApi itself) a
// replaced globalThis.fetch that is restored when the test ends.

const META: WebMeta = {
  service: 'lh-harness',
  api_version: '1',
  protocol_version: '1',
  capabilities: {},
  endpoint: 'test',
  agents: [{ id: 'claude_code' }, { id: 'codex' }],
  models: {
    claude_code: [
      { id: 'glm-5.2', label: 'GLM 5.2' },
      { id: 'glm-5.3' },
    ],
    codex: [{ id: 'gpt-5' }],
  },
  defaults: { agent: 'claude_code' },
};

const PENDING: QueueTaskEntry = {
  queue_id: 'q1',
  name: 'Fix flaky queue test',
  task: 'Fix the flaky queue test',
  status: 'pending',
  trio: 'glm',
  model: null,
  roles: { executor: { agent: 'claude_code', model: 'glm-5.2' } },
};

const LAUNCHED: QueueTaskEntry = {
  queue_id: 'q2',
  name: 'Ship the settings page',
  task: 'Ship the settings page',
  status: 'launched',
  trio: 'kimi',
  model: 'kimi-k2',
  roles: null,
  run_id: 'run-9',
};

interface StubOptions {
  entries?: QueueTaskEntry[];
  fetchError?: unknown;
  saveError?: unknown;
}

function stubApi(options: StubOptions = {}): { api: QueueTasksApi; saves: Array<{ queueId: string; patch: Record<string, unknown> }> } {
  const saves: Array<{ queueId: string; patch: Record<string, unknown> }> = [];
  const api: QueueTasksApi = {
    fetchQueueTasks: async () => {
      if (options.fetchError) throw options.fetchError;
      return { entries: options.entries ?? [PENDING, LAUNCHED] };
    },
    saveQueueTask: async (queueId, patch) => {
      if (options.saveError) throw options.saveError;
      saves.push({ queueId, patch: patch as Record<string, unknown> });
      return {
        ok: true,
        queue_id: queueId,
        status: 'pending',
        changed: true,
        model: 'model' in patch ? (patch.model ?? null) : null,
        roles: 'roles' in patch ? ((patch.roles as QueueTaskEntry['roles']) ?? null) : null,
        trio: 'glm',
      };
    },
  };
  return { api, saves };
}

function recorder(): { actions: QueueTasksAction[]; dispatch: (action: QueueTasksAction) => void } {
  const actions: QueueTasksAction[] = [];
  return { actions, dispatch: (action) => actions.push(action) };
}

function run(state: QueueTasksState, actions: QueueTasksAction[]): QueueTasksState {
  return actions.reduce(queueTasksReducer, state);
}

async function loadedState(api: QueueTasksApi): Promise<QueueTasksState> {
  const { actions, dispatch } = recorder();
  await loadQueueTasks(api, dispatch);
  return run(initialQueueTasksState(), actions);
}

function editingState(base: QueueTasksState, entry: QueueTaskEntry, meta: WebMeta | null = META, extra: QueueTasksAction[] = []): QueueTasksState {
  return run(base, [{ type: 'edit', entry, meta }, ...extra]);
}

function render(state: QueueTasksState, meta: WebMeta | null = META): string {
  return renderToStaticMarkup(
    <UiLanguageProvider>
      <QueueTasksDrawer
        state={state}
        meta={meta}
        onClose={() => undefined}
        onReload={() => undefined}
        onEdit={() => undefined}
        onCancel={() => undefined}
        onChangeModel={() => undefined}
        onChangeRole={() => undefined}
        onSave={() => undefined}
        onClearRoles={() => undefined}
        onRefreshModels={async () => undefined}
      />
    </UiLanguageProvider>,
  );
}

test('pending entry opens the editor: model dropdown fed only by the live list, grouped by provider', async () => {
  const loaded = await loadedState(stubApi().api);
  const state = editingState(loaded, PENDING);
  const html = render(state);

  // One task-model select for this entry, options ONLY from the live list.
  assert.match(html, /<select aria-label="q1 model"/);
  assert.ok(html.includes('<optgroup label="claude_code">'), html);
  assert.ok(html.includes('<optgroup label="codex">'), html);
  assert.ok(html.includes('<option value="glm-5.3"'), html);
  assert.ok(html.includes('<option value="gpt-5"'), html);
  // The empty option is the "no override — resolved from the trio at launch" choice.
  assert.ok(html.includes('No override (resolved from the trio at launch)'), html);
  // The per-role editor is the New-task form's RoleRuntimePicker (three roles).
  assert.equal(html.match(/role-runtime-card/g)?.length, 3, html);
  // Refresh button re-fetches the live list; launched row stays read-only.
  assert.ok(html.includes('data-testid="queue-refresh-models"'), html);
  assert.ok(html.includes('data-testid="queue-locked-q2"'), html);
  assert.ok(!html.includes('aria-label="q2 model"'), html);
});

test('an empty live list leaves no model options at all (nothing hardcoded)', async () => {
  const emptyMeta: WebMeta = { ...META, models: {} };
  const loaded = await loadedState(stubApi().api);
  const state = editingState(loaded, PENDING, emptyMeta);
  const html = render(state, emptyMeta);
  assert.match(html, /<select aria-label="q1 model"/);
  assert.ok(!html.includes('<optgroup'), html);
  assert.ok(!html.includes('glm-5.3'), html);
  assert.ok(!html.includes('gpt-5'), html);
});

test('a stored override missing from the live list stays selectable and is marked', async () => {
  const entry: QueueTaskEntry = { ...PENDING, model: 'glm-4.6' };
  const loaded = await loadedState(stubApi({ entries: [entry] }).api);
  const state = editingState(loaded, entry);
  const html = render(state);
  assert.ok(html.includes('glm-4.6 (not in the live list)'), html);
});

test('model save PUTs the model key only when no role moved', async () => {
  const { api, saves } = stubApi();
  const loaded = await loadedState(api);
  const state = editingState(loaded, PENDING, META, [{ type: 'model', model: 'glm-5.3' }]);

  const { actions, dispatch } = recorder();
  await saveQueueTaskDraft(api, state, dispatch);
  assert.equal(saves.length, 1);
  assert.equal(saves[0].queueId, 'q1');
  assert.deepEqual(saves[0].patch, { model: 'glm-5.3' });
  assert.ok(!('roles' in saves[0].patch));

  const next = run(state, actions);
  assert.equal(next.editing, null);
  assert.equal(next.savedFor, 'q1');
  assert.equal(next.entries.find((item) => item.queue_id === 'q1')?.model, 'glm-5.3');
  assert.ok(render(next).includes('data-testid="queue-edit-saved"'));
});

test('roles edit via the reused picker lands in the roles key; untouched roles stay unpinned', async () => {
  const { api, saves } = stubApi();
  const loaded = await loadedState(api);
  const state = editingState(loaded, PENDING, META, [
    { type: 'role', role: 'executor', value: { agent: 'claude_code', model: 'glm-5.3', custom: false, reasoning_effort: '', effortCustom: false } },
  ]);

  const { dispatch } = recorder();
  await saveQueueTaskDraft(api, state, dispatch);
  assert.equal(saves.length, 1);
  assert.deepEqual(saves[0].patch, {
    model: null,
    roles: { executor: { agent: 'claude_code', model: 'glm-5.3' } },
  });
  // manager/auditor drafts never left their seeds, so they are NOT sent.
  const patchRoles = saves[0].patch.roles as Record<string, unknown>;
  assert.ok(!('manager' in patchRoles));
  assert.ok(!('auditor' in patchRoles));
});

test('422 validation detail renders verbatim and the editor stays open', async () => {
  const api = stubApi({ saveError: new ApiError(422, "unknown model 'nope' in the live model list") }).api;
  const loaded = await loadedState(api);
  const state = editingState(loaded, PENDING, META, [{ type: 'model', model: 'nope' }]);

  const { actions, dispatch } = recorder();
  await saveQueueTaskDraft(api, state, dispatch);
  const next = run(state, actions);
  assert.equal(next.saveError, "unknown model 'nope' in the live model list");
  assert.equal(next.editing, 'q1');
  assert.equal(next.entries.find((item) => item.queue_id === 'q1')?.status, 'pending');
  const html = render(next);
  assert.ok(html.includes('data-testid="queue-edit-error"'), html);
  assert.ok(html.includes("unknown model &#x27;nope&#x27; in the live model list"), html);
  assert.match(html, /<select aria-label="q1 model"/);
});

test('409 after launch: the edit is refused verbatim and the entry renders locked', async () => {
  const api = stubApi({ saveError: new ApiError(409, 'cannot edit model/roles for status launched') }).api;
  const loaded = await loadedState(api);
  const state = editingState(loaded, PENDING, META, [{ type: 'model', model: 'glm-5.3' }]);

  const { actions, dispatch } = recorder();
  await saveQueueTaskDraft(api, state, dispatch);
  const next = run(state, actions);
  assert.equal(next.saveError, 'cannot edit model/roles for status launched');
  assert.equal(next.entries.find((item) => item.queue_id === 'q1')?.status, 'launched');
  const html = render(next);
  // The editor is gone; the row is the read-only locked form with the refusal.
  assert.ok(!html.includes('aria-label="q1 model"'), html);
  assert.ok(html.includes('data-testid="queue-locked-q1"'), html);
  assert.ok(html.includes('Launched — model and roles are locked'), html);
  assert.ok(html.includes('cannot edit model/roles for status launched'), html);
});

test('launched entries render read-only: locked indication, no edit affordance', async () => {
  const loaded = await loadedState(stubApi({ entries: [LAUNCHED] }).api);
  const html = render(loaded);
  assert.ok(html.includes('data-testid="queue-locked-q2"'), html);
  assert.ok(html.includes('run-9'), html);
  assert.ok(html.includes('kimi-k2'), html);
  assert.ok(!html.includes('Edit model/roles'), html);
  assert.ok(!html.includes('<select'), html);
});

test('403 on load renders the refusal instead of the list', async () => {
  const api = stubApi({ fetchError: new ApiError(403, 'this account is not on the admin list') }).api;
  const state = await loadedState(api);
  assert.equal(state.phase, 'refused');
  const html = render(state);
  assert.ok(html.includes('data-testid="queue-refused"'), html);
  assert.ok(html.includes('not on the admin list'), html);
  assert.ok(!html.includes('q1'), html);
});

test('clear role overrides sends roles: null in one write', async () => {
  const { api, saves } = stubApi();
  const loaded = await loadedState(api);
  const state = editingState(loaded, PENDING);
  await clearQueueTaskRoles(api, state, recorder().dispatch);
  assert.deepEqual(saves[0].patch, { roles: null });
});

test('pure helpers: pins, payload diffing and live-list grouping', async () => {
  assert.deepEqual(
    rolePinsFromSelection({ agent: 'claude_code', model: ' glm-5.3 ', custom: false, reasoning_effort: ' high ', effortCustom: false }, ' dev '),
    { agent: 'claude_code', model: 'glm-5.3', reasoning_effort: 'high', mcp_profile: 'dev' },
  );
  assert.deepEqual(
    rolePinsFromSelection({ agent: 'codex', model: '', custom: false, reasoning_effort: '', effortCustom: false }),
    { agent: 'codex' },
  );

  // Unchanged drafts produce no payload; a moved role carries its seed's mcp pin.
  const loaded = await loadedState(stubApi().api);
  const mcpEntry: QueueTaskEntry = { ...PENDING, roles: { manager: { model: 'glm-5.2', mcp_profile: 'dev' } } };
  const seeded = editingState(loaded, mcpEntry);
  assert.equal(buildQueueRolesPayload(seeded.draftRoles, seeded.roleSeeds, seeded.roleMcp), null);
  const moved = run(seeded, [{ type: 'role', role: 'manager', value: { ...seeded.draftRoles.manager, agent: 'codex' } }]);
  assert.deepEqual(buildQueueRolesPayload(moved.draftRoles, moved.roleSeeds, moved.roleMcp), {
    manager: { agent: 'codex', model: 'glm-5.2', mcp_profile: 'dev' },
  });

  const groups = modelOptionsFromMeta(META);
  assert.deepEqual(groups.map((group) => group.provider), ['claude_code', 'codex']);
  assert.deepEqual(groups[0].options.map((option) => option.id), ['glm-5.2', 'glm-5.3']);
  assert.deepEqual(modelOptionsFromMeta(null), []);
  assert.deepEqual(modelOptionsFromMeta({ ...META, models: { claude_code: [] } }), []);
});

test('browserQueueTasksApi hits the queue list and the R7 endpoint with the admin write guard', async () => {
  const calls: Array<{ url: string; init?: { method?: string; headers?: Record<string, string>; body?: unknown } }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: unknown, init?: { method?: string; headers?: Record<string, string>; body?: unknown }) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();
    calls.push({ url, init: { method, headers: init?.headers, body: init?.body } });
    const route = `${method} ${url}`;
    if (route === 'GET /api/queue?status=pending,launched') return fakeJsonResponse(200, { entries: [PENDING] });
    if (route === 'PUT /api/queue/q1/model') {
      return fakeJsonResponse(200, { ok: true, queue_id: 'q1', status: 'pending', changed: true, model: 'glm-5.3', roles: { executor: { agent: 'claude_code' } }, trio: 'glm' });
    }
    return fakeJsonResponse(500, { detail: 'unstubbed route' });
  }) as typeof fetch;
  try {
    const list = await browserQueueTasksApi.fetchQueueTasks();
    assert.deepEqual(list.entries, [PENDING]);
    const result = await browserQueueTasksApi.saveQueueTask('q1', { model: 'glm-5.3', roles: { executor: { agent: 'claude_code' } } });
    assert.equal(result.model, 'glm-5.3');

    const put = calls.find((call) => call.url === '/api/queue/q1/model' && call.init?.method === 'PUT');
    assert.ok(put, JSON.stringify(calls));
    assert.equal(put.init?.headers?.['X-Requested-With'], 'lh-harness');
    assert.deepEqual(JSON.parse(String(put.init?.body)), { model: 'glm-5.3', roles: { executor: { agent: 'claude_code' } } });
  } finally {
    globalThis.fetch = originalFetch;
  }

  // The 422/409 detail text is what the drawer renders verbatim.
  const problems: Array<[number, string]> = [
    [422, "unknown model 'nope' in the live model list"],
    [409, 'cannot edit model/roles for status launched'],
  ];
  for (const [status, detail] of problems) {
    globalThis.fetch = (async () => fakeJsonResponse(status, { detail })) as typeof fetch;
    try {
      await assert.rejects(
        () => browserQueueTasksApi.saveQueueTask('q1', { model: 'nope' }),
        (reason: unknown) => {
          assert.ok(reason instanceof ApiError);
          assert.equal((reason as ApiError).status, status);
          assert.equal((reason as ApiError).message, detail);
          return true;
        },
      );
    } finally {
      globalThis.fetch = originalFetch;
    }
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
