import { useCallback, useEffect, useReducer, useState, type Dispatch } from 'react';
import {
  fetchAudit, fetchKeys, fetchSettings, fetchWhoami, saveSetting, unsetSetting,
  type AdminAuditEntry, type AdminKey, type AdminSetting, type AdminWhoami,
} from './adminApi';
import {
  ApiError, authHeaders, fetchMeta, getJson, refreshModels,
  type ModelChoice, type WebMeta,
} from './api';

type AdminTab = 'settings' | 'keys' | 'queue' | 'audit';

const TABS: { id: AdminTab; label: string; path: string }[] = [
  { id: 'settings', label: 'Settings', path: '/admin' },
  { id: 'keys', label: 'Key restrictions', path: '/admin/keys' },
  { id: 'queue', label: 'Queue', path: '/admin/queue' },
  { id: 'audit', label: 'Audit log', path: '/admin/audit' },
];

function tabFromPath(pathname: string): AdminTab {
  if (pathname.startsWith('/admin/keys')) return 'keys';
  if (pathname.startsWith('/admin/queue')) return 'queue';
  if (pathname.startsWith('/admin/audit')) return 'audit';
  return 'settings';
}

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

/** Admin screens for the embedded settings store. Secret values are write-only. */
export default function AdminApp() {
  const [tab, setTab] = useState<AdminTab>(() => tabFromPath(window.location.pathname));
  const [who, setWho] = useState<AdminWhoami | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchWhoami().then(setWho).catch((reason) => setError(message(reason)));
  }, []);

  const go = (next: AdminTab) => {
    setTab(next);
    const path = TABS.find((t) => t.id === next)?.path || '/admin';
    window.history.replaceState(null, '', path);
  };

  return (
    <div className="admin-shell">
      <header className="admin-header">
        <h1>lh-harness admin</h1>
        <a href="/" className="admin-back">Back to the workbench</a>
        <nav className="admin-tabs" role="tablist" aria-label="Admin sections">
          {TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? 'active' : ''} onClick={() => go(t.id)}>{t.label}</button>
          ))}
        </nav>
      </header>
      {error && <div className="admin-error" role="alert">{error}</div>}
      {who && !who.is_admin && (
        <div className="admin-error" role="alert" data-testid="admin-refused">
          Admin access refused{who.identity ? ` for ${who.identity}` : ''}: {who.reason || 'not an admin'}.
        </div>
      )}
      {who?.is_admin && (
        <>
          <p className="admin-muted">Signed in as {who.identity}. Settings store: {who.store.path}. Service started {who.started_at}.</p>
          {tab === 'settings' && <SettingsPanel />}
          {tab === 'keys' && <KeysPanel />}
          {tab === 'queue' && <QueuePanel onShowAudit={() => go('audit')} />}
          {tab === 'audit' && <AuditPanel />}
        </>
      )}
    </div>
  );
}

function SettingsPanel() {
  const [rows, setRows] = useState<AdminSetting[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [newName, setNewName] = useState('');

  const load = useCallback(() => {
    fetchSettings().then((d) => { setRows(d.settings); setError(null); }).catch((reason) => setError(message(reason)));
  }, []);
  useEffect(load, [load]);

  const pendingRestart = rows.some((r) => r.changed_since_start && r.restart_required);

  return (
    <section className="admin-panel">
      {pendingRestart && (
        <div className="admin-restart" role="status">Some settings changed since the service started. Restart lh-harness (with no active run) to apply them.</div>
      )}
      {error && <div className="admin-error">{error}</div>}
      <table className="admin-table">
        <thead><tr><th>Name</th><th>Value</th><th>Source</th><th>Last change</th><th /></tr></thead>
        <tbody>
          {rows.map((row) => (
            <SettingRow key={row.name} row={row} editing={editing === row.name} onEdit={() => setEditing(editing === row.name ? null : row.name)} onDone={() => { setEditing(null); load(); }} />
          ))}
        </tbody>
      </table>
      <div className="admin-add">
        <input aria-label="New setting name" placeholder="NEW_SETTING_NAME" value={newName} onChange={(e) => setNewName(e.target.value.toUpperCase())} />
        <button type="button" disabled={!/^[A-Z][A-Z0-9_]{1,63}$/.test(newName)} onClick={() => { setEditing(newName); setRows((r) => r.some((x) => x.name === newName) ? r : [...r, { name: newName, description: 'Custom setting', secret: /TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL/.test(newName), set: false, source: 'unset', restart_required: true, changed_since_start: false, catalog: false }]); setNewName(''); }}>Add setting</button>
      </div>
    </section>
  );
}

function SettingRow({ row, editing, onEdit, onDone }: { row: AdminSetting; editing: boolean; onEdit: () => void; onDone: () => void }) {
  const [value, setValue] = useState('');
  const [note, setNote] = useState(row.note || '');
  const [expires, setExpires] = useState(row.expires_at || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);

  useEffect(() => {
    // A secret is never prefilled; a plain value starts from what is stored.
    setValue(row.secret ? '' : (row.value || ''));
    setNote(row.note || '');
    setExpires(row.expires_at || '');
    setError(null);
  }, [editing, row]);

  const shown = row.secret
    ? (row.set ? (row.source === 'db' ? `set (${row.length} chars)` : 'set in the environment') : 'not set')
    : (row.set ? (row.value || '') : 'not set');

  const save = async () => {
    setBusy(true); setError(null);
    try {
      const input: { value?: string; note?: string; expires_at?: string } = { note, expires_at: expires };
      if (value) input.value = value;
      const result = await saveSetting(row.name, input);
      setSavedNote(result.restart_required ? 'Saved. Restart lh-harness to apply it.' : 'Saved.');
      onDone();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(false);
    }
  };

  const unset = async () => {
    setBusy(true); setError(null);
    try {
      const result = await unsetSetting(row.name);
      setSavedNote(result.restart_required ? 'Removed. Restart lh-harness to apply it.' : 'Removed.');
      onDone();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <tr data-testid={`setting-${row.name}`}>
        <td><code>{row.name}</code>{row.secret && <span className="admin-badge">secret</span>}<div className="admin-muted">{row.description}</div></td>
        <td className={row.secret ? 'admin-muted' : ''}>{shown}{row.changed_since_start && row.restart_required && <span className="admin-badge warn">restart needed</span>}</td>
        <td>{row.source}</td>
        <td>{row.updated_by ? `${row.updated_by}, ${row.updated_at}` : '-'}{savedNote && <div className="admin-muted">{savedNote}</div>}</td>
        <td><button type="button" onClick={onEdit}>{editing ? 'Close' : (row.secret ? (row.set ? 'Rotate' : 'Set') : 'Edit')}</button></td>
      </tr>
      {editing && (
        <tr className="admin-edit-row">
          <td colSpan={5}>
            <label>
              {row.secret ? 'New value (write-only, never shown again)' : 'Value'}
              <input
                type={row.secret ? 'password' : 'text'}
                autoComplete={row.secret ? 'new-password' : 'off'}
                spellCheck={false}
                value={value}
                onChange={(e) => setValue(e.target.value)}
                aria-label={`${row.name} value`}
              />
            </label>
            <label>Note (scope, owner)<input value={note} onChange={(e) => setNote(e.target.value)} aria-label={`${row.name} note`} /></label>
            <label>Expires (YYYY-MM-DD)<input value={expires} onChange={(e) => setExpires(e.target.value)} aria-label={`${row.name} expiry`} /></label>
            <div className="admin-actions">
              <button type="button" disabled={busy || (!value && !row.set)} onClick={() => void save()}>Save</button>
              {row.source === 'db' && <button type="button" disabled={busy} onClick={() => void unset()}>Remove from the store</button>}
              {row.restart_required && <span className="admin-muted">Takes effect after an lh-harness restart.</span>}
            </div>
            {error && <div className="admin-error">{error}</div>}
          </td>
        </tr>
      )}
    </>
  );
}

function KeysPanel() {
  const [keys, setKeys] = useState<AdminKey[]>([]);
  const [node, setNode] = useState('');
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    fetchKeys().then((d) => { setKeys(d.keys); setNode(d.node); }).catch((reason) => setError(message(reason)));
  }, []);
  return (
    <section className="admin-panel">
      <p className="admin-muted">Every key and caller this node ({node}) knows, with its restrictions. Values are never shown.</p>
      {error && <div className="admin-error">{error}</div>}
      <table className="admin-table">
        <thead><tr><th>Key</th><th>State</th><th>Restrictions</th><th>Expiry</th></tr></thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.name} data-testid={`key-${k.name}`}>
              <td><code>{k.name}</code><div className="admin-muted">{k.description}</div>{k.note && <div className="admin-muted">Note: {k.note}</div>}</td>
              <td>{k.set ? `set (${k.source})` : 'not set'}</td>
              <td><Restrictions value={k.restrictions} /></td>
              <td>{k.expires_at || 'none recorded'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Restrictions({ value }: { value: Record<string, unknown> }) {
  return (
    <dl className="admin-restrictions">
      {Object.entries(value).map(([key, item]) => (
        <div key={key}>
          <dt>{key.replace(/_/g, ' ')}</dt>
          <dd>{renderRestriction(item)}</dd>
        </div>
      ))}
    </dl>
  );
}

function renderRestriction(item: unknown): string {
  if (item === null || item === undefined) return 'none';
  if (typeof item === 'boolean') return item ? 'yes' : 'no';
  if (Array.isArray(item)) {
    if (!item.length) return 'none';
    return item.map((x) => (x && typeof x === 'object' && 'name' in x)
      ? `${String((x as { name: unknown }).name)}: ${Array.isArray((x as { servers?: unknown }).servers) ? ((x as { servers: string[] }).servers.join(', ') || 'no servers') : String((x as { servers?: unknown }).servers)}`
      : String(x)).join('; ');
  }
  return String(item);
}

function AuditPanel() {
  const [entries, setEntries] = useState<AdminAuditEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    fetchAudit().then((d) => setEntries(d.entries)).catch((reason) => setError(message(reason)));
  }, []);
  return (
    <section className="admin-panel">
      {error && <div className="admin-error">{error}</div>}
      <table className="admin-table">
        <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Setting</th></tr></thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.id}><td>{e.at}</td><td>{e.actor}</td><td>{e.action}</td><td><code>{e.name}</code></td></tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// --------------------------- queue settings (trios + capacity) --------------
// LHH-QUEUE-HOTRELOAD (deliverable 5, part 1): trio defaults and per-trio
// capacity live in the settings store (queue_trio/queue_capacity); the R4
// endpoints (GET/PUT /api/queue/trios, GET/PUT /api/queue/capacity) are the
// ONLY persistence path used here, so their per-change audit rows land in the
// same audit log the Audit tab shows.  The launcher re-reads the store on
// every scheduling pass, so a save applies to the next launch with no
// restart; a running run keeps the model it started with.  The model
// dropdowns are drawn from the backend's live model list via the SAME
// endpoints the new-task form's "Role runtime configuration" uses (GET
// /api/meta, plus POST /api/models/refresh behind "Refresh models") and are
// grouped by the provider/agent keys the meta response supplies -- no model
// id is hardcoded anywhere in this panel.

/** One queue trio row as GET /api/queue/trios reports it. */
export interface QueueTrio {
  name: string;
  agent: string;
  model: string | null;
  mcp_profile: string | null;
  enabled: boolean;
  updated_by?: string;
  updated_at?: string;
}

/** The editable form of a trio row; '' means null (config.toml fallback). */
export interface QueueTrioEdit {
  name: string;
  agent: string;
  model: string;
  mcp_profile: string;
  enabled: boolean;
}

export interface QueueSettingsState {
  phase: 'loading' | 'ready' | 'refused' | 'error';
  loadError: string | null;
  /** Live model list as the backend sees it: provider/agent id -> choices. */
  models: Record<string, ModelChoice[]>;
  /** Provider/agent ids from the live meta response (dropdown options). */
  agents: string[];
  trios: QueueTrio[];
  edits: QueueTrioEdit[];
  capacityEdits: Record<string, string>;
  capacityPeakEdits: Record<string, string>;
  audit: AdminAuditEntry[];
  refreshing: boolean;
  refreshError: string | null;
  savingTrios: boolean;
  triosError: string | null;
  triosSaved: string | null;
  savingCapacity: boolean;
  capacityError: string | null;
  capacitySaved: string | null;
}

export type QueueAction =
  | { type: 'loaded'; trios: QueueTrio[]; capacity: Record<string, number>; maxPeak?: Record<string, number | null>; meta: WebMeta; audit: AdminAuditEntry[] }
  | { type: 'load-failed'; error: string; refused: boolean }
  | { type: 'edit-trio'; index: number; patch: Partial<QueueTrioEdit> }
  | { type: 'edit-capacity'; name: string; value: string }
  | { type: 'edit-capacity-peak'; name: string; value: string }
  | { type: 'refresh-started' }
  | { type: 'refreshed'; meta: WebMeta }
  | { type: 'refresh-failed'; error: string }
  | { type: 'trios-save-started' }
  | { type: 'trios-saved'; trios: QueueTrio[] }
  | { type: 'trios-save-failed'; error: string }
  | { type: 'capacity-save-started' }
  | { type: 'capacity-saved'; capacity: Record<string, number> }
  | { type: 'capacity-save-failed'; error: string };

/** Fetch seams the panel needs.  Tests inject stubs; the browser binds fetch. */
export interface QueueApi {
  /** The new-task form's Role-runtime model-list endpoint (GET /api/meta). */
  queueMeta: () => Promise<WebMeta>;
  /** The new-task form's Refresh-models endpoint (POST /api/models/refresh). */
  refreshModels: () => Promise<WebMeta>;
  fetchQueueTrios: () => Promise<{ trios: QueueTrio[] }>;
  saveQueueTrios: (trios: unknown[]) => Promise<{ trios: QueueTrio[] }>;
  fetchQueueCapacity: () => Promise<{ capacity: Record<string, number>; max_peak?: Record<string, number | null> }>;
  saveQueueCapacity: (capacity: Record<string, number | { max: number; max_peak: number | null }>) => Promise<{ capacity: Record<string, number>; max_peak?: Record<string, number | null> }>;
  /** The existing settings-audit endpoint (queue rows live in the same store). */
  fetchQueueAudit: (limit?: number) => Promise<{ entries: AdminAuditEntry[] }>;
}

/** One admin write with the cross-site-form guard the server requires. */
async function queueWrite<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'PUT',
    headers: {
      ...authHeaders(),
      // Required by the server for every admin write (blocks cross-site forms).
      'X-Requested-With': 'lh-harness',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json() as { detail?: unknown };
      if (payload && typeof payload.detail === 'string' && payload.detail.trim()) detail = payload.detail.trim();
    } catch {
      // Keep the status line.
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const browserQueueApi: QueueApi = {
  queueMeta: fetchMeta,
  refreshModels,
  fetchQueueTrios: () => getJson<{ trios: QueueTrio[] }>('/api/queue/trios'),
  saveQueueTrios: (trios) => queueWrite<{ trios: QueueTrio[] }>('/api/queue/trios', { trios }),
  fetchQueueCapacity: () => getJson<{ capacity: Record<string, number> }>('/api/queue/capacity'),
  saveQueueCapacity: (capacity) => queueWrite<{ capacity: Record<string, number>; max_peak?: Record<string, number | null> }>('/api/queue/capacity', { capacity }),
  fetchQueueAudit: (limit = 50) => fetchAudit(limit),
};

export function initialQueueSettingsState(): QueueSettingsState {
  return {
    phase: 'loading',
    loadError: null,
    models: {},
    agents: [],
    trios: [],
    edits: [],
    capacityEdits: {},
    audit: [],
    refreshing: false,
    refreshError: null,
    savingTrios: false,
    triosError: null,
    triosSaved: null,
    savingCapacity: false,
    capacityError: null,
    capacitySaved: null,
    capacityPeakEdits: {},
  };
}

export function modelsFromMeta(meta: WebMeta): Record<string, ModelChoice[]> {
  return meta.models || {};
}

export function agentsFromMeta(meta: WebMeta): string[] {
  return (meta.agents || []).map((agent) => agent.id);
}

export function trioEditsFromRows(trios: QueueTrio[]): QueueTrioEdit[] {
  return trios.map((row) => ({
    name: row.name,
    agent: row.agent,
    model: row.model || '',
    mcp_profile: row.mcp_profile || '',
    enabled: row.enabled,
  }));
}

/**
 * The PUT /api/queue/trios body rows built from the edits.  Only the five
 * fields the endpoint accepts are sent (updated_by/updated_at never round
 * trip), and '' maps to null so the launcher falls back per key to
 * config.toml.  Models stay exactly as loaded or as picked from the live
 * list; membership is the endpoint's all-or-nothing validation, which the
 * panel surfaces as the 422 detail text.
 */
export function triosPayloadFromEdits(edits: QueueTrioEdit[]): QueueTrio[] {
  return edits.map((row) => ({
    name: row.name,
    agent: row.agent,
    model: row.model.trim() || null,
    mcp_profile: row.mcp_profile.trim() || null,
    enabled: row.enabled,
  }));
}

/** Client-side guard mirroring the endpoint's 422; the API is never called on failure.
 *
 * ``peaks`` is optional: a blank/absent entry sends the rich form with
 * ``max_peak: null`` (clears any stored peak override ONLY when a peak input
 * exists for that trio); a numeric entry sends the lower peak cap applied
 * Mon-Fri 06:00-10:00 UTC.
 */
export function capacityPayloadFromEdits(
  edits: Record<string, string>,
  peaks?: Record<string, string>,
): { capacity: Record<string, number | { max: number; max_peak: number | null }> } | { error: string } {
  const capacity: Record<string, number | { max: number; max_peak: number | null }> = {};
  for (const [name, raw] of Object.entries(edits)) {
    const value = raw.trim();
    if (!/^\d+$/.test(value)) return { error: `capacity for '${name}' must be a non-negative integer` };
    const base = Number.parseInt(value, 10);
    const peakRaw = (peaks?.[name] ?? '').trim();
    if (peakRaw === '') {
      // No peak input on the row: legacy bare-int form — the stored peak
      // override (if any) is preserved server-side.
      capacity[name] = base;
      continue;
    }
    if (!/^\d+$/.test(peakRaw)) return { error: `peak capacity for '${name}' must be blank or a non-negative integer` };
    const peak = Number.parseInt(peakRaw, 10);
    if (peak > base) return { error: `peak capacity for '${name}' cannot exceed the all-day max (${base})` };
    capacity[name] = { max: base, max_peak: peak };
  }
  return { capacity };
}

/** Queue-related rows of the shared settings audit log (R4 trios/capacity, R7 entry edits). */
export function queueAuditEntries(entries: AdminAuditEntry[], limit = 10): AdminAuditEntry[] {
  return entries.filter((entry) => entry.name.startsWith('queue_')).slice(0, limit);
}

export function queueSettingsReducer(state: QueueSettingsState, action: QueueAction): QueueSettingsState {
  switch (action.type) {
    case 'loaded':
      return {
        ...state,
        phase: 'ready',
        loadError: null,
        models: modelsFromMeta(action.meta),
        agents: agentsFromMeta(action.meta),
        trios: action.trios,
        edits: trioEditsFromRows(action.trios),
        capacityEdits: Object.fromEntries(Object.entries(action.capacity).map(([name, value]) => [name, String(value)])),
        capacityPeakEdits: Object.fromEntries(
          Object.entries(action.capacity).map(([name]) => [name, action.maxPeak && action.maxPeak[name] != null ? String(action.maxPeak[name]) : '']),
        ),
        audit: action.audit,
      };
    case 'load-failed':
      return { ...state, phase: action.refused ? 'refused' : 'error', loadError: action.error };
    case 'edit-trio': {
      const current = state.edits[action.index];
      if (!current) return state;
      const next = { ...current, ...action.patch };
      // A model belongs to its provider; changing the agent clears the model
      // so the save cannot carry a stale id into the endpoint's 422 check.
      if (action.patch.agent !== undefined && action.patch.agent !== current.agent) next.model = '';
      const edits = state.edits.slice();
      edits[action.index] = next;
      return { ...state, edits };
    }
    case 'edit-capacity-peak':
      return { ...state, capacityPeakEdits: { ...state.capacityPeakEdits, [action.name]: action.value } };
    case 'edit-capacity':
      return { ...state, capacityEdits: { ...state.capacityEdits, [action.name]: action.value } };
    case 'refresh-started':
      return { ...state, refreshing: true, refreshError: null };
    case 'refreshed':
      return { ...state, refreshing: false, refreshError: null, models: modelsFromMeta(action.meta), agents: agentsFromMeta(action.meta) };
    case 'refresh-failed':
      return { ...state, refreshing: false, refreshError: action.error };
    case 'trios-save-started':
      return { ...state, savingTrios: true, triosError: null, triosSaved: null };
    case 'trios-saved':
      return { ...state, savingTrios: false, trios: action.trios, edits: trioEditsFromRows(action.trios), triosSaved: 'Saved.' };
    case 'trios-save-failed':
      return { ...state, savingTrios: false, triosError: action.error };
    case 'capacity-save-started':
      return { ...state, savingCapacity: true, capacityError: null, capacitySaved: null };
    case 'capacity-saved':
      return {
        ...state,
        savingCapacity: false,
        capacityEdits: Object.fromEntries(Object.entries(action.capacity).map(([name, value]) => [name, String(value)])),
        capacitySaved: 'Saved.',
      };
    case 'capacity-save-failed':
      return { ...state, savingCapacity: false, capacityError: action.error };
    default:
      return state;
  }
}

export async function loadQueueSettings(api: QueueApi, dispatch: Dispatch<QueueAction>): Promise<void> {
  try {
    const [triosRes, capacityRes, meta, auditRes] = await Promise.all([
      api.fetchQueueTrios(),
      api.fetchQueueCapacity(), // shape: {capacity, max_peak}
      api.queueMeta(),
      api.fetchQueueAudit(50),
    ]);
    dispatch({ type: 'loaded', trios: triosRes.trios, capacity: capacityRes.capacity, maxPeak: capacityRes.max_peak, meta, audit: auditRes.entries });
  } catch (reason) {
    dispatch({ type: 'load-failed', error: message(reason), refused: reason instanceof ApiError && reason.status === 403 });
  }
}

export async function refreshQueueModels(api: QueueApi, dispatch: Dispatch<QueueAction>): Promise<void> {
  dispatch({ type: 'refresh-started' });
  try {
    dispatch({ type: 'refreshed', meta: await api.refreshModels() });
  } catch (reason) {
    dispatch({ type: 'refresh-failed', error: message(reason) });
  }
}

export async function saveQueueTrios(api: QueueApi, edits: QueueTrioEdit[], dispatch: Dispatch<QueueAction>): Promise<void> {
  const payload = triosPayloadFromEdits(edits);
  dispatch({ type: 'trios-save-started' });
  try {
    // The endpoint validates every row against the live list before writing
    // anything; a refusal (422 unknown model, 503 live list down) changes
    // nothing server-side, so the edits stay exactly as the user left them.
    const result = await api.saveQueueTrios(payload);
    dispatch({ type: 'trios-saved', trios: result.trios });
  } catch (reason) {
    dispatch({ type: 'trios-save-failed', error: message(reason) });
  }
}

export async function saveQueueCapacity(api: QueueApi, edits: Record<string, string>, peaks: Record<string, string>, dispatch: Dispatch<QueueAction>): Promise<void> {
  const parsed = capacityPayloadFromEdits(edits, peaks);
  if ('error' in parsed) {
    dispatch({ type: 'capacity-save-failed', error: parsed.error });
    return;
  }
  dispatch({ type: 'capacity-save-started' });
  try {
    const result = await api.saveQueueCapacity(parsed.capacity);
    dispatch({ type: 'capacity-saved', capacity: result.capacity });
  } catch (reason) {
    dispatch({ type: 'capacity-save-failed', error: message(reason) });
  }
}

export function QueueSettingsView({ state, onShowAudit, onRefreshModels, onEditTrio, onEditCapacity, onEditCapacityPeak, onSaveTrios, onSaveCapacity }: {
  state: QueueSettingsState;
  onShowAudit: () => void;
  onRefreshModels: () => void;
  onEditTrio: (index: number, patch: Partial<QueueTrioEdit>) => void;
  onEditCapacity: (name: string, value: string) => void;
  onEditCapacityPeak: (name: string, value: string) => void;
  onSaveTrios: () => void;
  onSaveCapacity: () => void;
}) {
  if (state.phase === 'loading') {
    return <section className="admin-panel"><p className="admin-muted">Loading queue settings…</p></section>;
  }
  if (state.phase === 'refused') {
    return (
      <section className="admin-panel">
        <div className="admin-error" role="alert" data-testid="queue-refused">Queue settings require admin access{state.loadError ? `: ${state.loadError}` : '.'}</div>
      </section>
    );
  }
  if (state.phase === 'error') {
    return (
      <section className="admin-panel">
        <div className="admin-error" role="alert">Failed to load queue settings: {state.loadError}</div>
      </section>
    );
  }
  return (
    <section className="admin-panel" data-testid="queue-panel">
      <div className="admin-actions">
        <button type="button" disabled={state.refreshing} onClick={onRefreshModels} data-testid="refresh-models">
          {state.refreshing ? 'Refreshing…' : 'Refresh models'}
        </button>
        {state.refreshError && <span className="admin-error">{state.refreshError}</span>}
      </div>
      <p className="admin-muted">
        Trio defaults and per-trio concurrency caps from the settings store. The model dropdowns come from the
        backend&apos;s live model list — the same list the new-task form shows — grouped by provider; Refresh models
        re-fetches it. Saving applies to the next queue launch with no restart. A running run keeps the model it
        started with.
      </p>
      <table className="admin-table">
        <thead><tr><th>Trio</th><th>Agent</th><th>Model (live list)</th><th>MCP profile</th><th>Enabled</th><th>Last change</th></tr></thead>
        <tbody>
          {state.edits.map((edit, index) => {
            const stored = state.trios.find((row) => row.name === edit.name);
            const choices = state.models[edit.agent] || [];
            return (
              <tr key={edit.name} data-testid={`queue-trio-${edit.name}`}>
                <td><code>{edit.name}</code></td>
                <td>
                  <select aria-label={`${edit.name} agent`} value={edit.agent} onChange={(event) => onEditTrio(index, { agent: event.target.value })}>
                    {!state.agents.includes(edit.agent) && <option value={edit.agent}>{edit.agent} (not in the live agent list)</option>}
                    {state.agents.map((agent) => <option key={agent} value={agent}>{agent}</option>)}
                  </select>
                </td>
                <td>
                  <select aria-label={`${edit.name} model`} value={edit.model} onChange={(event) => onEditTrio(index, { model: event.target.value })}>
                    <option value="">(default — config.toml fallback)</option>
                    {choices.length > 0 && (
                      <optgroup label={edit.agent}>
                        {choices.map((choice) => <option key={choice.id} value={choice.id}>{choice.label || choice.id}</option>)}
                      </optgroup>
                    )}
                    {edit.model !== '' && !choices.some((choice) => choice.id === edit.model) && (
                      <option value={edit.model}>{edit.model} (not in the live list)</option>
                    )}
                  </select>
                </td>
                <td>
                  <input aria-label={`${edit.name} MCP profile`} value={edit.mcp_profile} placeholder="(none)" onChange={(event) => onEditTrio(index, { mcp_profile: event.target.value })} />
                </td>
                <td>
                  <input aria-label={`${edit.name} enabled`} type="checkbox" checked={edit.enabled} onChange={(event) => onEditTrio(index, { enabled: event.target.checked })} />
                </td>
                <td className="admin-muted">{stored?.updated_by ? `${stored.updated_by}, ${stored.updated_at}` : '—'}</td>
              </tr>
            );
          })}
          {!state.edits.length && <tr><td colSpan={6} className="admin-muted">No queue trios recorded in the settings store yet.</td></tr>}
        </tbody>
      </table>
      <div className="admin-actions">
        <button type="button" disabled={state.savingTrios} onClick={onSaveTrios} data-testid="save-trios">{state.savingTrios ? 'Saving…' : 'Save trios'}</button>
        {state.triosSaved && <span className="admin-muted" data-testid="queue-trios-saved">{state.triosSaved}</span>}
      </div>
      {state.triosError && <div className="admin-error" role="alert" data-testid="queue-trios-error">{state.triosError}</div>}
      <p className="admin-muted">Concurrent runs per trio, applied on the very next scheduling pass.</p>
      <table className="admin-table">
        <thead><tr><th>Trio</th><th>Max concurrent</th></tr></thead>
        <tbody>
          {Object.keys(state.capacityEdits).sort().map((name) => (
            <tr key={name} data-testid={`queue-capacity-${name}`}>
              <td><code>{name}</code></td>
              <td>
                <input aria-label={`${name} max concurrent`} type="number" min={0} step={1} value={state.capacityEdits[name]} onChange={(event) => onEditCapacity(name, event.target.value)} />
              </td>
              <td>
                <input aria-label={`${name} peak max (Mon-Fri 06:00-10:00 UTC, blank = no override)`} type="number" min={0} step={1} value={state.capacityPeakEdits[name] ?? ''} onChange={(event) => onEditCapacityPeak(name, event.target.value)} placeholder="—" />
              </td>
            </tr>
          ))}
          {!Object.keys(state.capacityEdits).length && <tr><td colSpan={3} className="admin-muted">No capacity caps recorded in the settings store yet.</td></tr>}
        </tbody>
      </table>
      <div className="admin-actions">
        <button type="button" disabled={state.savingCapacity || !Object.keys(state.capacityEdits).length} onClick={onSaveCapacity} data-testid="save-capacity">{state.savingCapacity ? 'Saving…' : 'Save capacity'}</button>
        {state.capacitySaved && <span className="admin-muted" data-testid="queue-capacity-saved">{state.capacitySaved}</span>}
      </div>
      {state.capacityError && <div className="admin-error" role="alert" data-testid="queue-capacity-error">{state.capacityError}</div>}
      <p className="admin-muted">Every save is written to the shared audit log by the queue endpoints.</p>
      <table className="admin-table" data-testid="queue-audit">
        <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Queue change</th></tr></thead>
        <tbody>
          {queueAuditEntries(state.audit).map((entry) => (
            <tr key={entry.id}><td>{entry.at}</td><td>{entry.actor}</td><td>{entry.action}</td><td><code>{entry.name}</code></td></tr>
          ))}
          {!queueAuditEntries(state.audit).length && <tr><td colSpan={4} className="admin-muted">No queue changes recorded yet.</td></tr>}
        </tbody>
      </table>
      <div className="admin-actions">
        <button type="button" onClick={onShowAudit}>Open the full audit log</button>
      </div>
    </section>
  );
}

/** Queue trios + capacity, re-read by the launcher every pass (LHH-QUEUE-HOTRELOAD). */
export function QueuePanel({ api, onShowAudit }: { api?: QueueApi; onShowAudit: () => void }) {
  const boundApi = api || browserQueueApi;
  const [state, dispatch] = useReducer(queueSettingsReducer, initialQueueSettingsState());
  useEffect(() => {
    void loadQueueSettings(boundApi, dispatch);
  }, [boundApi]);
  return (
    <QueueSettingsView
      state={state}
      onShowAudit={onShowAudit}
      onRefreshModels={() => void refreshQueueModels(boundApi, dispatch)}
      onEditTrio={(index, patch) => dispatch({ type: 'edit-trio', index, patch })}
      onEditCapacity={(name, value) => dispatch({ type: 'edit-capacity', name, value })}
      onEditCapacityPeak={(name, value) => dispatch({ type: 'edit-capacity-peak', name, value })}
      onSaveTrios={() => void saveQueueTrios(boundApi, state.edits, dispatch)}
      onSaveCapacity={() => void saveQueueCapacity(boundApi, state.capacityEdits, state.capacityPeakEdits, dispatch)}
    />
  );
}
