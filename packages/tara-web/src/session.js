// Operator session for the GCS API (issue #64).
//
// The token lives in sessionStorage: it survives a reload but not closing the
// tab, and it is per-tab, so two operators can use two tabs on one machine.
// Every GCS call goes through gcsFetch, which adds the bearer token and, on a
// 401 (expired, revoked, GCS restarted), drops the session so the app returns
// to the login screen instead of failing call by call.
import { useEffect, useState } from 'react';
import { API_BASE } from './config';

const STORAGE_KEY = 'tara_gcs_session';
const EXPIRED_EVENT = 'tara:session-ended';

/** The telemetry socket's auth subprotocol; see tara_gcs.main.WS_AUTH_PROTOCOL. */
export const WS_AUTH_PROTOCOL = 'tara.bearer';

export function getSession() {
  try {
    const s = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null');
    return s && s.token ? s : null;
  } catch {
    return null;
  }
}

export function setSession(session) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  } catch {
    // Storage blocked: the session lasts until the next 401 instead.
  }
  window.dispatchEvent(new Event(EXPIRED_EVENT));
}

export function clearSession() {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // nothing stored
  }
  window.dispatchEvent(new Event(EXPIRED_EVENT));
}

/** fetch() for GCS endpoints: adds the bearer token, ends the session on 401. */
export async function gcsFetch(url, options = {}) {
  const session = getSession();
  const headers = new Headers(options.headers || {});
  if (session) headers.set('Authorization', `Bearer ${session.token}`);
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401 && session) clearSession();
  return res;
}

export async function login(operator, passphrase) {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ operator, passphrase }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Login failed (HTTP ${res.status}).`);
  setSession({ token: body.token, operator: body.operator, role: body.role });
  return body;
}

export async function logout() {
  await gcsFetch(`${API_BASE}/api/auth/logout`, { method: 'POST' }).catch(() => {});
  clearSession();
}

/** The current session, re-rendering when it starts or ends. */
export function useSession() {
  const [session, setState] = useState(getSession);
  useEffect(() => {
    const sync = () => setState(getSession());
    window.addEventListener(EXPIRED_EVENT, sync);
    return () => window.removeEventListener(EXPIRED_EVENT, sync);
  }, []);
  return session;
}
