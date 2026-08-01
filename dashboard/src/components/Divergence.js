import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../lib';
import { GitBranch, Radio, RefreshCw } from 'lucide-react';

// Surfaces the single most valuable output SENTRIX v4 added: where the AI and
// the deterministic policy DISAGREED (v3 silently threw this away), plus the
// live real-time ingestion status so operators can see real telemetry landing.
export default function Divergence() {
  const [div, setDiv] = useState({ divergences: [], count: 0 });
  const [ingest, setIngest] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([
      api('/api/divergence?limit=30').catch(() => ({ divergences: [], count: 0 })),
      api('/api/ingest/status').catch(() => null),
    ]).then(([d, i]) => { setDiv(d); setIngest(i); setLoading(false); });
  }, []);

  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t); }, [load]);

  return (
    <div className="panel">
      <div className="panel-head" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div className="title"><GitBranch size={15} /> Reasoning Divergence &amp; Live Feed</div>
        <button className="btn ghost" onClick={load}><RefreshCw size={13} /> Refresh</button>
      </div>

      {/* Live ingestion status */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-title"><Radio size={14} /> Real-time ingestion</div>
        {ingest ? (
          <div className="kv-row" style={{ display: 'flex', gap: 24, flexWrap: 'wrap', fontSize: 13 }}>
            <span>Mode: <b style={{ color: 'var(--accent)' }}>{ingest.mode}</b></span>
            <span>Live assets: <b>{ingest.live?.live_assets ?? 0}</b></span>
            <span>Events: <b>{ingest.live?.events_total ?? 0}</b></span>
            <span>Dropped: <b>{ingest.live?.events_dropped ?? 0}</b></span>
            <span>Adapters: <b>{(ingest.live?.adapters || []).join(', ')}</b></span>
          </div>
        ) : <div className="muted">Ingestion status unavailable.</div>}
        <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
          Push real telemetry: <code>POST /api/ingest</code> (json · syslog · prometheus). Set <code>SENTRIX_MODE=live</code> to reason over it.
        </div>
      </div>

      {/* Divergences: AI decided differently than the rulebook */}
      <div className="card-title" style={{ marginBottom: 8 }}>
        AI vs deterministic policy — {div.count} flagged for review
      </div>
      {loading && !div.count ? (
        <div className="muted">Loading…</div>
      ) : div.divergences.length === 0 ? (
        <div className="muted">No divergences yet. When the model reasons to a different call than the rulebook (within safety bounds), it lands here instead of being silently overwritten.</div>
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>Asset</th><th>Model said</th><th>Final</th><th>Why</th></tr>
            </thead>
            <tbody>
              {div.divergences.map((d, i) => (
                <tr key={i}>
                  <td><b>{d.asset_name || d.asset_id}</b></td>
                  <td><span className="tag warn">{d.model_action || '—'}</span></td>
                  <td><span className="tag p2">{d.final_action}</span></td>
                  <td style={{ fontSize: 12, color: 'var(--muted)' }}>{d.note || d.explanation}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
