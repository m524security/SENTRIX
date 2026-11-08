import React, { useEffect, useState, useCallback } from 'react';
import { api, tierLabel } from '../lib';

export default function HeatMap() {
  const [risk, setRisk] = useState(null);
  const [datasets, setDatasets] = useState({});

  const load = useCallback(() => {
    api('/api/risk-summary').then(setRisk).catch(() => {});
    api('/api/datasets').then((d) => { if (!d.error) setDatasets(d); }).catch(() => {});
  }, []);
  useEffect(() => { load(); const iv = setInterval(load, 15000); return () => clearInterval(iv); }, [load]);

  const cellColor = (healthyPct, critical) => {
    if (critical > 0) return { bg: 'var(--p1-dim)', fg: 'var(--p1)', border: 'rgba(244,63,94,0.3)' };
    if (healthyPct >= 90) return { bg: 'var(--ok-dim)', fg: 'var(--ok)', border: 'rgba(45,212,167,0.3)' };
    if (healthyPct >= 70) return { bg: 'var(--warn-dim)', fg: 'var(--warn)', border: 'rgba(240,180,41,0.3)' };
    return { bg: 'var(--p2-dim)', fg: 'var(--p2)', border: 'rgba(249,115,22,0.3)' };
  };

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">Risk topology</div>
          <h2>Fleet risk map</h2>
        </div>
        <p className="desc">Health concentration across criticality tiers and dataset categories. Red cells carry active critical failures.</p>
      </div>

      {!risk ? <div className="loading"><div className="spinner" />building risk map…</div> : (
        <>
          <div className="card card-pad" style={{ marginBottom: 18 }}>
            <div className="eyebrow" style={{ marginBottom: 14 }}>By criticality tier</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px,1fr))', gap: 12 }}>
              {[1, 2, 3, 4].map((t) => {
                const td = risk[`tier_${t}`] || { total: 0, healthy: 0, critical: 0 };
                const pct = td.total ? (td.healthy / td.total) * 100 : 100;
                const c = cellColor(pct, td.critical);
                return (
                  <div key={t} className="heat-cell" style={{ background: c.bg, borderColor: c.border }}>
                    <div className="hc-lbl">{tierLabel(t)}</div>
                    <div className="hc-num" style={{ color: c.fg }}>{Math.round(pct)}%</div>
                    <div className="hc-lbl">{td.healthy}/{td.total} healthy{td.critical ? ` · ${td.critical} critical` : ''}</div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="card card-pad">
            <div className="eyebrow" style={{ marginBottom: 14 }}>By dataset · real observed error rate</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px,1fr))', gap: 12 }}>
              {Object.values(datasets).map((d) => {
                const er = d.real_observed_error_rate_pct;
                const c = er >= 40 ? cellColor(0, 1) : er >= 15 ? cellColor(75, 0) : cellColor(95, 0);
                return (
                  <div key={d.id} className="heat-cell" style={{ background: c.bg, borderColor: c.border }}>
                    <div className="hc-lbl" style={{ color: 'var(--text-dim)' }}>{d.label}</div>
                    <div className="hc-num" style={{ color: c.fg }}>{er}%</div>
                    <div className="hc-lbl">{d.asset_count} assets · {d.log_lines_analyzed} lines</div>
                  </div>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
