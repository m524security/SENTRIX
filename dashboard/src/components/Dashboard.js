import React, { useEffect, useState } from 'react';
import { api, actionClass, actionLabel, tierLabel, riskColor } from '../lib';
import { AlertTriangle, Shield, TrendingUp, BarChart3, Activity, CheckCircle } from 'lucide-react';

export default function Dashboard({ cycles = [], health }) {
  const [risk, setRisk] = useState(null);
  const [preds, setPreds] = useState(null);

  useEffect(() => {
    const load = () => {
      api('/api/risk-summary').then(setRisk).catch(() => {});
      api('/api/predictions').then(setPreds).catch(() => {});
    };
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, []);

  const latest = cycles[0];
  const decisions = latest?.decisions || [];
  const topRisks = [...decisions].sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0)).slice(0, 6);

  const successRate = health?.backup_success_rate ?? 0;
  const alerts = health?.active_alerts ?? 0;
  const highForecasts = preds?.high ?? 0;

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">Fleet overview</div>
          <h2>Recovery Readiness</h2>
        </div>
        <p className="desc">Live fleet state, refreshed as the agent completes cycles.</p>
      </div>

      {/* ── Bento metric cards ── */}
      <div className="metric-row">
        <MetricCard
          icon={<Shield size={18} />}
          iconColor="var(--ok)"
          label="Backup success rate"
          value={`${successRate}%`}
          foot={`${health?.total_assets ?? '—'} assets monitored`}
          severity={successRate > 80 ? 'ok' : 'warn'}
        />
        <MetricCard
          icon={<AlertTriangle size={18} />}
          iconColor="var(--p1)"
          label="Active alerts"
          value={alerts}
          foot="assets with 3+ consecutive failures"
          severity={alerts > 0 ? 'danger' : 'ok'}
        />
        <MetricCard
          icon={<TrendingUp size={18} />}
          iconColor="var(--warn)"
          label="Forecast breaches"
          value={highForecasts}
          foot="predicted to breach RPO soon"
          severity={highForecasts > 0 ? 'warn' : 'ok'}
        />
        <MetricCard
          icon={<BarChart3 size={18} />}
          iconColor="var(--accent)"
          label="Cycles recorded"
          value={health?.cycles_recorded ?? cycles.length}
          foot={latest ? (latest.fallback_mode ? 'last: rule engine' : `last: ${latest.provider || 'llm'}`) : 'awaiting first cycle'}
        />
      </div>

      {/* ── Main bento grid ── */}
      <div className="grid-2">
        <div className="card">
          <div className="card-head">
            <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Activity size={14} style={{ color: 'var(--accent)' }} />
              Highest-Risk Assets
            </h3>
            <span className="eyebrow">{latest ? `cycle ${latest.cycle_id}` : 'no cycle yet'}</span>
          </div>
          {topRisks.length === 0 ? (
            <div className="empty">
              <div className="ico"><Shield size={28} /></div>
              No cycle data yet. Run a simulation or start the agent.
            </div>
          ) : (
            <div className="tbl-wrap" style={{ border: 'none' }}>
              <table className="tbl">
                <thead><tr><th>Asset</th><th>Tier</th><th>RPO</th><th>Risk</th><th>Action</th></tr></thead>
                <tbody>
                  {topRisks.map((d) => (
                    <tr key={d.asset_id}>
                      <td className="id">{d.asset_id}</td>
                      <td><span className="tag tier">T{d.tier}</span></td>
                      <td className="num">{Math.round(d.rpo_percentage)}%</td>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <div className="riskbar"><span style={{ width: `${Math.min(100, d.risk_score / 6)}%`, background: riskColor(d.risk_score) }} /></div>
                          <span className="mono" style={{ fontSize: 11, color: 'var(--text-dim)' }}>{Math.round(d.risk_score)}</span>
                        </div>
                      </td>
                      <td><span className={`tag ${actionClass(d.action)}`}>{actionLabel(d.action)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <BarChart3 size={14} style={{ color: 'var(--violet)' }} />
              Risk by Tier
            </h3>
            <span className="pill live" style={{ fontSize: 10, padding: '2px 8px' }}>
              <span className="dot" />live
            </span>
          </div>
          <div className="card-pad">
            {!risk ? <div className="loading"><div className="spinner" />loading risk map…</div> : (
              [1, 2, 3, 4].map((t) => {
                const td = risk[`tier_${t}`] || { total: 0, healthy: 0, critical: 0 };
                const pct = td.total ? (td.healthy / td.total) * 100 : 100;
                return (
                  <div key={t} style={{ marginBottom: 16 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8, fontSize: 12 }}>
                      <span className="mono" style={{ color: 'var(--text-dim)', fontWeight: 500 }}>{tierLabel(t)}</span>
                      <span className="mono" style={{ color: td.critical ? 'var(--p1)' : 'var(--text-faint)', fontSize: 11 }}>
                        {td.healthy}/{td.total} healthy{td.critical ? ` · ${td.critical} critical` : ''}
                      </span>
                    </div>
                    <div className="riskbar" style={{ height: 8 }}>
                      <span style={{ width: `${pct}%`, background: pct > 80 ? 'var(--ok)' : pct > 50 ? 'var(--warn)' : 'var(--p1)' }} />
                    </div>
                  </div>
                );
              })
            )}
            {latest?.summary && (
              <div style={{ marginTop: 20, paddingTop: 18, borderTop: '1px solid var(--line)' }}>
                <div className="eyebrow" style={{ marginBottom: 8 }}>Agent Summary</div>
                <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.6, fontWeight: 400 }}>{latest.summary}</div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function MetricCard({ icon, iconColor, label, value, foot, severity }) {
  return (
    <div className={`metric ${severity || ''}`} style={{ position: 'relative' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
        <div className="m-label">{label}</div>
        <div style={{
          width: 32, height: 32, borderRadius: 8,
          display: 'grid', placeItems: 'center',
          background: `color-mix(in srgb, ${iconColor || 'var(--accent)'} 10%, transparent)`,
          color: iconColor || 'var(--accent)',
          border: `1px solid color-mix(in srgb, ${iconColor || 'var(--accent)'} 15%, transparent)`,
        }}>
          {icon}
        </div>
      </div>
      <div className={`m-value ${severity || ''}`}>{value}</div>
      <div className="m-foot">{foot}</div>
    </div>
  );
}
