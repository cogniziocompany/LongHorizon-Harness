import { useCallback, useEffect, useState } from 'react';
import {
  fetchAudit, fetchKeys, fetchSettings, fetchWhoami, saveSetting, unsetSetting,
  type AdminAuditEntry, type AdminKey, type AdminSetting, type AdminWhoami,
} from './adminApi';

type AdminTab = 'settings' | 'keys' | 'audit';

const TABS: { id: AdminTab; label: string; path: string }[] = [
  { id: 'settings', label: 'Settings', path: '/admin' },
  { id: 'keys', label: 'Key restrictions', path: '/admin/keys' },
  { id: 'audit', label: 'Audit log', path: '/admin/audit' },
];

function tabFromPath(pathname: string): AdminTab {
  if (pathname.startsWith('/admin/keys')) return 'keys';
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
