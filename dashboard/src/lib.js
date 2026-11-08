// Shared helpers — single source of truth for the dashboard's API base,
// fetch wrapper, and the severity/action vocabulary so every component
// colour-codes identically.

export const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';

// The /ws feed streams live asset names, criticality and cycle telemetry, so it
// is token-gated whenever the API has a token configured (always, in
// production). Set REACT_APP_WS_TOKEN at build time, or drop a token into
// localStorage under `sentrix_token` at runtime — the latter is handy for demos
// where you don't want to rebuild the bundle.
export function wsToken() {
  try {
    return localStorage.getItem('sentrix_token') || process.env.REACT_APP_WS_TOKEN || '';
  } catch {
    return process.env.REACT_APP_WS_TOKEN || '';
  }
}

export function wsUrl(path = '/ws') {
  const base = API_BASE.replace(/^http/, 'ws');
  const t = wsToken();
  return `${base}${path}${t ? `?token=${encodeURIComponent(t)}` : ''}`;
}

export async function api(path, options = {}) {
  const timeout = options._timeout || 120000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(`${API_BASE}${path}`, { ...options, signal: controller.signal });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') throw new Error('Request timed out — the AI model is still loading. Try again in a moment.');
    throw e;
  }
}

// Map an action string to its severity class (matches the CSS .tag.* names).
export function actionClass(action) {
  switch (action) {
    case 'ESCALATE_P1': return 'p1';
    case 'ESCALATE_P2': return 'p2';
    case 'WARN':
    case 'MANUAL_REVIEW': return 'warn';
    case 'NONE': return 'none';
    case 'SCHEDULE_RESTORE_TEST':
    case 'RETRY_BACKUP': return 'info';
    default: return 'info';
  }
}

export function actionLabel(action) {
  const map = {
    ESCALATE_P1: 'P1', ESCALATE_P2: 'P2', WARN: 'WARN', NONE: 'OK',
    SCHEDULE_RESTORE_TEST: 'RESTORE TEST', RETRY_BACKUP: 'RETRY', MANUAL_REVIEW: 'REVIEW',
  };
  return map[action] || action;
}

export function riskColor(score) {
  if (score >= 501) return 'var(--p1)';
  if (score >= 200) return 'var(--p2)';
  if (score >= 50) return 'var(--warn)';
  return 'var(--ok)';
}

export function tierLabel(tier) {
  return { 1: 'T1 · Critical', 2: 'T2 · High', 3: 'T3 · Standard', 4: 'T4 · Low' }[tier] || `T${tier}`;
}

export function fmtTime(iso) {
  if (!iso) return '--:--:--';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return iso; }
}
