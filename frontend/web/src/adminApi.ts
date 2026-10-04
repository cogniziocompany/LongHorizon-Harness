import { ApiError, authHeaders, getJson } from './api';

/** One setting as the admin API reports it. Secret settings never carry `value`. */
export interface AdminSetting {
  name: string;
  description: string;
  secret: boolean;
  set: boolean;
  source: 'db' | 'env' | 'unset';
  restart_required: boolean;
  changed_since_start: boolean;
  catalog: boolean;
  value?: string;
  length?: number;
  note?: string;
  expires_at?: string | null;
  updated_at?: string;
  updated_by?: string;
}

export interface AdminWhoami {
  identity: string | null;
  is_admin: boolean;
  reason: string | null;
  store: { configured: boolean; error: string | null; path: string | null };
  started_at: string;
}

export interface AdminAuditEntry { id: number; at: string; actor: string; action: string; name: string; detail: Record<string, unknown> }

export interface AdminKey {
  name: string;
  description: string;
  set: boolean;
  source: 'db' | 'env' | 'unset';
  expires_at: string | null;
  note: string;
  node: string;
  restrictions: Record<string, unknown>;
}

export const fetchWhoami = () => getJson<AdminWhoami>('/api/admin/whoami');
export const fetchSettings = () => getJson<{ settings: AdminSetting[]; started_at: string }>('/api/admin/settings');
export const fetchAudit = (limit = 200) => getJson<{ entries: AdminAuditEntry[] }>(`/api/admin/audit?limit=${limit}`);
export const fetchKeys = () => getJson<{ node: string; keys: AdminKey[] }>('/api/admin/keys');

async function adminWrite(method: 'PUT' | 'DELETE', path: string, body?: unknown): Promise<{ restart_required?: boolean }> {
  const response = await fetch(path, {
    method,
    headers: {
      ...authHeaders(),
      // Required by the server for every admin write (blocks cross-site forms).
      'X-Requested-With': 'lh-harness',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json() as { detail?: unknown };
      if (typeof payload.detail === 'string') message = payload.detail;
    } catch { /* keep the status line */ }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<{ restart_required?: boolean }>;
}

/** Set or rotate a value. The value is sent once and never read back. */
export const saveSetting = (name: string, input: { value?: string; note?: string; expires_at?: string; secret?: boolean }) =>
  adminWrite('PUT', `/api/admin/settings/${encodeURIComponent(name)}`, input);

export const unsetSetting = (name: string) => adminWrite('DELETE', `/api/admin/settings/${encodeURIComponent(name)}`);
